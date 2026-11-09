"""Test-only pipeline stage handlers run by the real worker subprocess (T3.5 tests).

They record each execution in ``data_root/runs.log`` and chain to the next stage exactly like
the production handlers, using the worker's settings snapshot.
"""

from __future__ import annotations

from chronos.enums import JobKind
from chronos.jobs.worker import JobContext
from chronos.pipeline import StagePayload, _chain_next, _deps  # pyright: ignore[reportPrivateUsage]


def _stage(ctx: JobContext, stage: JobKind) -> None:
    payload = StagePayload.model_validate_json(ctx.payload_json)
    with (ctx.data_root / "runs.log").open("a") as f:
        f.write(f"{stage.value} {payload.chain}\n")
    deps = _deps(ctx)  # reads (and deletes) the worker's 0600 settings snapshot
    try:
        _chain_next(deps, payload, stage)
    finally:
        deps.engine.dispose()


def ok_ingest(ctx: JobContext) -> None:
    _stage(ctx, JobKind.ingest)


def ok_script(ctx: JobContext) -> None:
    _stage(ctx, JobKind.script)


def failing_script(ctx: JobContext) -> None:
    raise RuntimeError("script stage exploded")


def ok_assets(ctx: JobContext) -> None:
    _stage(ctx, JobKind.assets)


def ok_tts(ctx: JobContext) -> None:
    _stage(ctx, JobKind.tts)


def ok_captions(ctx: JobContext) -> None:
    _stage(ctx, JobKind.captions)


def ok_render(ctx: JobContext) -> None:
    _stage(ctx, JobKind.render)
