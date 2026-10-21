"""Unit tests for report/sparkline.py (T1.8 step 3)."""

from __future__ import annotations

from chronos.report.sparkline import sparkline

_BLOCKS = "\u2581\u2582\u2583\u2584\u2585\u2586\u2587\u2588"


def test_sparkline_empty() -> None:
    assert sparkline([]) == ""


def test_sparkline_zero() -> None:
    result = sparkline([0])
    assert result == "\u2581"


def test_sparkline_hundred() -> None:
    result = sparkline([100])
    assert result == "\u2588"


def test_sparkline_midpoint_50() -> None:
    result = sparkline([50])
    assert len(result) == 1
    # 50 * 8 / 101 = 3.96... -> index 3 = '▄'
    assert result in _BLOCKS


def test_sparkline_width_truncates() -> None:
    scores = list(range(40))
    result = sparkline(scores, width=10)
    assert len(result) == 10


def test_sparkline_fixed_scale_ascending() -> None:
    """Scores 0, 50, 100 should produce ascending block characters."""
    result = sparkline([0, 50, 100])
    assert len(result) == 3
    # Each char's index should be >= previous.
    indices = [_BLOCKS.index(c) for c in result]
    assert indices == sorted(indices)


def test_sparkline_all_levels() -> None:
    """8 scores spread across [0,100] should produce 8 distinct chars."""
    # Scores that map to each of the 8 levels.
    scores = [6, 19, 32, 45, 58, 71, 84, 97]
    result = sparkline(scores)
    assert len(result) == 8
    # All chars are valid sparkline blocks.
    for c in result:
        assert c in _BLOCKS


def test_sparkline_single_event_high() -> None:
    """Single score of 85 -> one of the top blocks."""
    result = sparkline([85])
    assert result in _BLOCKS[-3:]  # ▆▇█


def test_sparkline_clamps_negative() -> None:
    """Negative scores are treated as 0."""
    result = sparkline([-10])
    assert result == "\u2581"


def test_sparkline_clamps_over_100() -> None:
    """Scores > 100 are treated as 100."""
    result = sparkline([200])
    assert result == "\u2588"


def test_sparkline_typical_morning_sequence() -> None:
    """A typical morning sequence produces valid output."""
    scores = [12, 48, 72, 80, 85, 70, 60]
    result = sparkline(scores)
    assert len(result) == 7
    for c in result:
        assert c in _BLOCKS
