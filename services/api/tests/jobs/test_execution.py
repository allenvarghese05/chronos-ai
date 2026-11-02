"""Actual process behavior, bounded pipe drains, environment/cwd and inherited flock."""

from __future__ import annotations

import os
import sys
import threading
from pathlib import Path

import pytest

from chronos.jobs.ownership import ExecutionLock
from chronos.media.subprocess import run


def test_output_flood_and_minimal_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRIVATE_PROCESS_CANARY", "should-not-be-in-child")
    program = (
        "import os,sys;assert 'PRIVATE_PROCESS_CANARY' not in os.environ;"
        "sys.stdout.buffer.write(b'a'*300000+b'\\xff');"
        "sys.stderr.buffer.write(b'b'*300000+b'\\xfe')"
    )
    result = run(
        [sys.executable, "-c", program],
        timeout=5,
        cwd=tmp_path,
        data_root=tmp_path,
        output_limit=1024,
    )
    assert result.returncode == 0 and not result.timed_out
    assert len(result.stdout) == len(result.stderr) == 1024


def test_real_sleep_timeout_and_cancellation(tmp_path: Path) -> None:
    result = run(
        [sys.executable, "-c", "import time;time.sleep(30)"],
        timeout=0.15,
        cwd=tmp_path,
        data_root=tmp_path,
    )
    assert result.timed_out and result.returncode != 0 and result.duration_ms < 2000
    stop = threading.Event()
    stop.set()
    result = run(
        [sys.executable, "-c", "import time;time.sleep(30)"],
        timeout=5,
        cwd=tmp_path,
        data_root=tmp_path,
        cancel=stop,
    )
    assert result.cancelled and result.duration_ms < 2000


def test_symlink_cwd_and_environment_rejected(tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    (root / "escape").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError):
        run([sys.executable], timeout=1, cwd=root / "escape", data_root=root)
    with pytest.raises(ValueError):
        run([sys.executable], timeout=1, cwd=root, data_root=root, env={"SECRET_KEY": "never"})


def test_lock_alias_and_inherited_fd_lifetime(tmp_path: Path) -> None:
    root = tmp_path / "data"
    root.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    first, second = ExecutionLock(root), ExecutionLock(alias)
    assert first.acquire() and first.fd is not None
    inherited = os.dup(first.fd)
    first.close()
    assert not second.acquire()
    os.close(inherited)
    assert second.acquire()
    second.close()


def test_lock_symlink_refused(tmp_path: Path) -> None:
    (tmp_path / "jobs").mkdir()
    target = tmp_path / "other"
    target.touch()
    (tmp_path / "jobs" / "execution.lock").symlink_to(target)
    with pytest.raises(OSError):
        ExecutionLock(tmp_path).acquire()
