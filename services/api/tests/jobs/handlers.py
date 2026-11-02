"""Trusted test-only handlers; never registered by the application production registry."""

from __future__ import annotations

import json
import os
import signal
import sys
import time

from chronos.jobs.worker import JobContext
from chronos.media.subprocess import run


def success(context: JobContext) -> None:
    (context.data_root / "success").write_text(str(context.job_id))


def failing(context: JobContext) -> None:
    raise RuntimeError("PRIVATE_HANDLER_CANARY must not be stored or logged")


def sleeper(context: JobContext) -> None:
    (context.data_root / "sleeper.pid").write_text(str(os.getpid()))
    time.sleep(30)


def interval(context: JobContext) -> None:
    def record(state: str) -> None:
        with (context.data_root / "intervals").open("a") as stream:
            stream.write(json.dumps([state, context.job_id, time.monotonic()]) + "\n")

    record("start")
    time.sleep(0.3)
    record("end")


def crash_first(context: JobContext) -> None:
    marker = context.data_root / "crash-first.pid"
    if not marker.exists():
        marker.write_text(str(os.getpid()))
        time.sleep(30)
    (context.data_root / "retried").write_text("done")


def descendants(context: JobContext) -> None:
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    program = (
        "import os,signal,subprocess,sys,time;from pathlib import Path;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        "child=subprocess.Popen([sys.executable,'-c',"
        "'import signal,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);time.sleep(30)']);"
        f"Path({str(context.data_root / 'descendants.pid')!r})"
        ".write_text(str(os.getpid())+','+str(child.pid));"
        "time.sleep(30)"
    )
    run(
        [sys.executable, "-c", program],
        timeout=10,
        cwd=context.work_dir,
        data_root=context.data_root,
    )
