"""Wake-engine utilities: window computation and local-day functions (T1.3).

All symbols are pure (no I/O, no DB, no Settings).  Import from here or
directly from :mod:`chronos.wake.window`.
"""

from __future__ import annotations

from chronos.wake.window import (
    DayKind as DayKind,
)
from chronos.wake.window import (
    Window as Window,
)
from chronos.wake.window import (
    WindowConfig as WindowConfig,
)
from chronos.wake.window import (
    day_kind as day_kind,
)
from chronos.wake.window import (
    is_in_window as is_in_window,
)
from chronos.wake.window import (
    local_date_for as local_date_for,
)
from chronos.wake.window import (
    next_window_start as next_window_start,
)
from chronos.wake.window import (
    tick_instants as tick_instants,
)
from chronos.wake.window import (
    window_for as window_for,
)

__all__ = [
    "DayKind",
    "Window",
    "WindowConfig",
    "day_kind",
    "is_in_window",
    "local_date_for",
    "next_window_start",
    "tick_instants",
    "window_for",
]
