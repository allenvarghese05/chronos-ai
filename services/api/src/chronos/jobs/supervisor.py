"""Ephemeral killable attempt guardian; no public API accepts this internal request format."""

from __future__ import annotations

import ctypes
import importlib
import json
import os
import select
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import cast

from chronos.jobs.worker import JobContext
from chronos.media.subprocess import configure_attempt, group_alive, signal_group, start_assertion


def _reap() -> None:
    while True:
        try:
            pid, _ = os.waitpid(-1, os.WNOHANG)
            if pid == 0:
                return
        except ChildProcessError:
            return


def main() -> None:
    """Load only a trusted registry-selected handler, retain ownership through actual cleanup."""
    request_path, lock_raw, control_raw = sys.argv[1:]
    lock_fd, control_fd = int(lock_raw), int(control_raw)
    raw = request_path and Path(request_path).read_bytes()
    if len(raw) > 65536:
        raise ValueError("invalid internal request bound")
    request = cast(dict[str, object], json.loads(raw))
    directory = Path(str(request["work_dir"]))
    root = Path(str(request["data_root"])).resolve()
    if not directory.resolve().is_relative_to(root):
        raise ValueError("invalid internal working directory")
    deadline = float(str(request["deadline"]))
    for path in reversed(cast(list[str], request["module_paths"])):
        if path and path not in sys.path:
            sys.path.insert(0, path)
    if sys.platform.startswith("linux"):
        # PR_SET_CHILD_SUBREAPER lets us reap orphan tool descendants instead of relying on PID1.
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(36, 1, 0, 0, 0) != 0:
            raise OSError("cannot supervise descendants")
    assertion: subprocess.Popen[bytes] | None = None
    group: int | None = None
    result = "spawn_failed"
    reason_path = directory / "outcome"
    try:
        assertion = start_assertion(directory, lock_fd)
        if select.select([control_fd], [], [], 0)[0] or time.monotonic() >= deadline:
            result = "timeout" if time.monotonic() >= deadline else "interrupted"
        else:
            pid = os.fork()
            if pid == 0:
                os.setpgid(0, 0)
                sink = os.open(os.devnull, os.O_WRONLY)
                os.dup2(sink, 1)
                os.dup2(sink, 2)
                os.close(sink)
                configure_attempt(lock_fd, reason_path)
                context = JobContext(
                    int(str(request["job_id"])),
                    str(request["owner"]),
                    str(request["payload_json"]),
                    root,
                    directory,
                )
                try:
                    target = cast(
                        Callable[[JobContext], None],
                        getattr(
                            importlib.import_module(str(request["module"])),
                            str(request["function"]),
                        ),
                    )
                    target(context)
                except BaseException:
                    os._exit(1)
                os._exit(0)
            group = pid
            # Both sides set the group, closing the startup race before cancellation can signal it.
            try:
                os.setpgid(pid, pid)
            except ProcessLookupError:
                pass
            while True:
                finished, status = os.waitpid(pid, os.WNOHANG)
                if finished:
                    result = "done" if os.waitstatus_to_exitcode(status) == 0 else "handler_failed"
                    if reason_path.exists():
                        reason = reason_path.read_text()[:32]
                        if reason in {"timeout", "interrupted"}:
                            result = reason
                    break
                if time.monotonic() >= deadline:
                    result = "timeout"
                    break
                if select.select([control_fd], [], [], 0.02)[0]:
                    result = "timeout" if time.monotonic() >= deadline else "interrupted"
                    break
    finally:
        if group is not None:
            signal_group(group, signal.SIGTERM)
            until = time.monotonic() + 0.2
            while group_alive(group) and time.monotonic() < until:
                _reap()
                time.sleep(0.01)
            signal_group(group, signal.SIGKILL)
            # Fail closed: retain flock until every tracked group member is gone/reaped.
            while group_alive(group):
                _reap()
                time.sleep(0.01)
            _reap()
        if assertion is not None:
            assertion.terminate()
            try:
                assertion.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                assertion.kill()
                assertion.wait()
            if assertion.stdout is not None:
                assertion.stdout.close()
        os.close(control_fd)
    try:
        os.write(1, (result + "\n").encode("ascii"))
    except BrokenPipeError:
        pass
    os.close(lock_fd)


if __name__ == "__main__":
    main()
