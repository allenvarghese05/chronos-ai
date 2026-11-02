"""Single argv/cwd/environment/process boundary for trusted job and future media tools.

No captured output is logged automatically. A supervised handler uses its attempt group;
timeouts terminate that whole attempt, allowing the guardian to prove cleanup before retry.
"""

from __future__ import annotations

import contextvars
import json
import math
import os
import selectors
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

_LOCK: contextvars.ContextVar[int | None] = contextvars.ContextVar("attempt_lock", default=None)
_REASON: contextvars.ContextVar[Path | None] = contextvars.ContextVar(
    "attempt_reason", default=None
)
_ENV_KEYS = {"PATH", "LANG", "LC_ALL", "TZ", "OMP_NUM_THREADS", "MKL_NUM_THREADS"}
Outcome = Literal[
    "done", "handler_failed", "timeout", "interrupted", "spawn_failed", "cleanup_uncertain"
]
_OUTCOMES = {"done", "handler_failed", "timeout", "interrupted", "spawn_failed"}


@dataclass(frozen=True)
class ProcessResult:
    """Bounded retained output; callers must not expose raw bytes to logs or HTTP."""

    returncode: int
    stdout: bytes
    stderr: bytes
    duration_ms: int
    timed_out: bool = False
    cancelled: bool = False


@dataclass(frozen=True)
class ExecutionOutcome:
    """Fixed safe result protocol, without raw handler exceptions or output."""

    status: Outcome
    duration_ms: int


def configure_attempt(lock_fd: int, reason_path: Path) -> None:
    """Internal supervisor-only context; trusted tool descendants inherit the ownership FD."""
    _LOCK.set(lock_fd)
    _REASON.set(reason_path)


def _cwd(cwd: Path, data_root: Path) -> Path:
    root = data_root.resolve()
    resolved = cwd.resolve(strict=True)
    if not resolved.is_dir() or not resolved.is_relative_to(root):
        raise ValueError("process working directory must be inside data root")
    return resolved


def _environment(cwd: Path, supplied: Mapping[str, str] | None = None) -> dict[str, str]:
    result = {"PATH": os.defpath, "HOME": str(cwd), "TMPDIR": str(cwd), "LANG": "C.UTF-8"}
    if supplied:
        for key, value in supplied.items():
            if key not in _ENV_KEYS or "\0" in value or len(value) > 4096:
                raise ValueError("process environment field is not allowed")
            result[key] = value
    return result


def _argv(argv: object) -> None:
    if not isinstance(argv, list):
        raise ValueError("invalid process argument list")
    values = cast(list[object], argv)
    if (
        not values
        or len(values) > 256
        or any(not isinstance(arg, str) or "\0" in arg or len(arg) > 4096 for arg in values)
        or sum(len(str(arg)) for arg in values) > 65536
    ):
        raise ValueError("invalid process argument list")


def group_alive(group: int) -> bool:
    """Probe only a process group started and tracked by this execution boundary."""
    try:
        os.killpg(group, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        # macOS can deny a probe while a terminated child awaits reaping. Permission
        # uncertainty is never proof of absence: keep ownership until a later ESRCH.
        return True


def signal_group(group: int, sig: int) -> None:
    """Signal only the tracked attempt group; database text/PIDs are never accepted here."""
    try:
        os.killpg(group, sig)
    except ProcessLookupError:
        pass
    except PermissionError:
        # The guardian's liveness loop retains flock if signaling/cleanup is uncertain.
        pass


def _spawn(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    pass_fds: tuple[int, ...] = (),
    own_group: bool = True,
    stderr: int = subprocess.PIPE,
) -> subprocess.Popen[bytes]:
    _argv(argv)
    return subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=stderr,
        start_new_session=own_group,
        close_fds=True,
        pass_fds=pass_fds,
    )


def run(
    argv: list[str],
    *,
    timeout: float,
    cwd: Path,
    data_root: Path,
    env: Mapping[str, str] | None = None,
    cancel: threading.Event | None = None,
    output_limit: int = 65536,
) -> ProcessResult:
    """Run trusted argv under data, drain bounded output, terminate/reap on deadline or stop.

    In a supervised handler a timeout stops the entire attempt group, including this Python
    handler. The guardian reports timeout and performs all descendant cleanup before retry.
    """
    _argv(argv)
    if not math.isfinite(timeout) or not 0 < timeout <= 3600 or not 0 <= output_limit <= 65536:
        raise ValueError("invalid process bounds")
    directory = _cwd(cwd, data_root)
    inherited = _LOCK.get()
    process = _spawn(
        argv,
        cwd=directory,
        env=_environment(directory, env),
        pass_fds=() if inherited is None else (inherited,),
        own_group=inherited is None,
    )
    started = time.monotonic()
    timed_out = stopped = False
    buffers: dict[str, bytearray] = {"stdout": bytearray(), "stderr": bytearray()}
    with selectors.DefaultSelector() as selector:
        assert process.stdout is not None and process.stderr is not None
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        try:
            while selector.get_map() or process.poll() is None:
                timed_out = time.monotonic() - started >= timeout
                stopped = cancel is not None and cancel.is_set()
                if timed_out or stopped:
                    reason = _REASON.get()
                    if inherited is not None:
                        if reason is not None:
                            reason.write_text("timeout" if timed_out else "interrupted")
                        signal_group(os.getpgrp(), signal.SIGTERM)
                        # A handler ignoring TERM is still killed by its guardian deadline.
                        signal_group(os.getpgrp(), signal.SIGKILL)
                    signal_group(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=0.2)
                    except subprocess.TimeoutExpired:
                        pass
                    signal_group(process.pid, signal.SIGKILL)
                    break
                for key, _ in selector.select(timeout=0.02):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        buffer = buffers[str(key.data)]
                        buffer.extend(chunk[: max(0, output_limit - len(buffer))])
        finally:
            if inherited is None:
                # A tool that exited while leaving children behind is not allowed to leak them.
                signal_group(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    signal_group(process.pid, signal.SIGKILL)
                signal_group(process.pid, signal.SIGKILL)
            process.wait()
            process.stdout.close()
            process.stderr.close()
    return ProcessResult(
        process.returncode,
        bytes(buffers["stdout"]),
        bytes(buffers["stderr"]),
        int((time.monotonic() - started) * 1000),
        timed_out,
        stopped,
    )


def start_assertion(cwd: Path, lock_fd: int) -> subprocess.Popen[bytes] | None:
    """Managed macOS idle-sleep assertion, tied to the guardian's true lifetime."""
    if sys.platform != "darwin":
        return None
    return _spawn(
        ["/usr/bin/caffeinate", "-i", "-w", str(os.getpid())],
        cwd=cwd,
        env=_environment(cwd),
        pass_fds=(lock_fd,),
        own_group=False,
        stderr=subprocess.DEVNULL,
    )


def run_handler(
    request: dict[str, object],
    *,
    cwd: Path,
    data_root: Path,
    lock_fd: int,
    cancel: threading.Event,
    timeout: float,
) -> ExecutionOutcome:
    """Supervise a trusted handler; liveness EOF survives worker SIGKILL, unlike a finally block."""
    directory = _cwd(cwd, data_root)
    started = time.monotonic()
    request_path = directory / "request.json"
    descriptor = os.open(request_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    request["deadline"] = started + timeout
    # The guardian runs under data/, so even the parent interpreter's empty/current path
    # must be frozen as an absolute trusted code-search path before changing cwd.
    request["module_paths"] = [str(Path(path or ".").resolve()) for path in sys.path]
    encoded = json.dumps(request, allow_nan=False).encode()
    if len(encoded) > 65536:
        os.close(descriptor)
        request_path.unlink()
        raise ValueError("supervisor request exceeds bound")
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(encoded)
    reader, writer = os.pipe()
    process: subprocess.Popen[bytes] | None = None
    try:
        process = _spawn(
            [
                sys.executable,
                "-m",
                "chronos.jobs.supervisor",
                str(request_path),
                str(lock_fd),
                str(reader),
            ],
            cwd=directory,
            env=_environment(directory),
            pass_fds=(lock_fd, reader),
            stderr=subprocess.DEVNULL,
        )
        os.close(reader)
        reader = -1
        assert process.stdout is not None
        output = bytearray()
        cleanup_deadline = started + timeout + 4
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while process.poll() is None:
                if cancel.is_set() or time.monotonic() - started >= timeout:
                    if writer >= 0:
                        os.close(writer)
                        writer = -1
                        cleanup_deadline = min(cleanup_deadline, time.monotonic() + 3)
                if time.monotonic() > cleanup_deadline:
                    # Do not kill the lock-holding guardian when cleanup cannot be proved.
                    return ExecutionOutcome(
                        "cleanup_uncertain", int((time.monotonic() - started) * 1000)
                    )
                for key, _ in selector.select(0.02):
                    chunk = os.read(key.fd, 256)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        output.extend(chunk[: max(0, 128 - len(output))])
            output.extend(process.stdout.read(128 - len(output)))
        status = output.decode("ascii", errors="replace").strip()
        if status not in _OUTCOMES:
            status = "spawn_failed"
        # The small fixed protocol is the only guardian stdout; handler stdout is /dev/null.
        from typing import cast

        return ExecutionOutcome(cast(Outcome, status), int((time.monotonic() - started) * 1000))
    except OSError:
        return ExecutionOutcome("spawn_failed", int((time.monotonic() - started) * 1000))
    finally:
        if reader >= 0:
            os.close(reader)
        if writer >= 0:
            os.close(writer)
        if process is not None and process.stdout is not None:
            process.stdout.close()
        # Keep the request until the guardian is gone; it may not yet have read it on a slow boot.
        if process is None or process.poll() is not None:
            request_path.unlink(missing_ok=True)
