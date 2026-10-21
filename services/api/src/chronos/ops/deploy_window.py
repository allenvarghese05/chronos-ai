"""Deploy-window guard — pure function (T0.6).

The deploy exclusion window is::

    [window_start - LEAD_MIN, window_end)

where ``LEAD_MIN = 45`` and all times are in local wall-clock time for the
calendar date derived from *now*.

Called by ``chronos ops in-deploy-window`` (exit 0 = in window, exit 1 = not)
and by ``scripts/deploy.sh`` via the CLI.

This module has **no I/O**; all probes are passed as arguments so tests can
inject a fixed clock without monkeypatching.
"""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

from chronos.wake.rules import WakeRules
from chronos.wake.window import window_for

LEAD_MIN: int = 45
"""Minutes before window_start that the deploy exclusion begins."""

_DEFAULT_TZ = ZoneInfo("America/Los_Angeles")


def in_deploy_window(
    now: datetime.datetime,
    rules: WakeRules,
    tz: ZoneInfo | None = None,
) -> bool:
    """Return ``True`` if *now* is inside the deploy exclusion window.

    The exclusion window is ``[window_start - LEAD_MIN, window_end)`` in local
    wall-clock time.  This prevents deployments from interrupting a morning
    reel pipeline.

    Args:
        now: A **tz-aware** UTC datetime.  Raises ``ValueError`` for naive inputs.
        rules: Active :class:`~chronos.wake.rules.WakeRules` parsed from
            ``config/wake_rules.yaml``.
        tz: Local timezone.  Defaults to ``America/Los_Angeles``.

    Returns:
        ``True`` if a deploy should be **skipped** (inside exclusion window).
        ``False`` if a deploy is safe to proceed.

    Raises:
        ValueError: If *now* has no tzinfo.

    Example::

        >>> import datetime
        >>> from zoneinfo import ZoneInfo
        >>> from chronos.ops.deploy_window import in_deploy_window
        >>> # 05:30 LA time, weekday window starts 06:00 → exclusion starts 05:15
        >>> now = datetime.datetime(2024, 1, 15, 13, 30, tzinfo=ZoneInfo("UTC"))
        >>> # result depends on the active rules window
    """
    if now.tzinfo is None:
        raise ValueError("now must be tz-aware; got a naive datetime")

    local_tz = tz or _DEFAULT_TZ
    local_dt = now.astimezone(local_tz)
    local_date = local_dt.date()

    wc = rules.window.to_window_config()
    win = window_for(local_date, local_tz, wc)

    # win.start_local / win.end_local are naive datetime.time values.
    # Build tz-aware exclusion bounds on the same local date.
    window_start_dt = datetime.datetime.combine(local_date, win.start_local, tzinfo=local_tz)
    window_end_dt = datetime.datetime.combine(local_date, win.end_local, tzinfo=local_tz)
    excl_start = window_start_dt - datetime.timedelta(minutes=LEAD_MIN)
    excl_end = window_end_dt  # half-open: [excl_start, excl_end)

    return excl_start <= local_dt < excl_end
