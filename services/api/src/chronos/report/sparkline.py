"""Unicode sparkline for wake-score timelines (T1.8)."""

from __future__ import annotations

_BLOCKS = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"  # ▁▂▃▄▅▆▇█
_N = len(_BLOCKS)  # 8


def sparkline(scores: list[int], width: int = 30) -> str:
    """Return a unicode sparkline on a fixed 0-100 scale.

    Each score in [0, 100] maps to one of 8 block characters (``\u2581``...``\u2588``).
    The output is exactly ``min(len(scores), width)`` characters.
    An empty list returns an empty string.

    Args:
        scores: List of integer scores in [0, 100].
        width:  Maximum number of characters to output (right-truncates).

    Returns:
        Unicode sparkline string (e.g. ``'\u2581\u2583\u2585\u2588'``).

    Examples::

        sparkline([0, 25, 50, 75, 100])   # '\u2581\u2583\u2585\u2587\u2588'
        sparkline([])                      # ''
        sparkline([100], width=1)          # '\u2588'
    """
    if not scores:
        return ""
    visible = scores[:width]

    def _char(s: int) -> str:
        # Fixed 0-100 scale: divide [0,100] into 8 equal bands.
        idx = min(int(max(0, min(100, s)) * _N / 101), _N - 1)
        return _BLOCKS[idx]

    return "".join(_char(s) for s in visible)
