"""Wake-engine replay and comparison engine (T1.8).

Replays stored or fixture mornings under two rule versions and returns
per-day comparison summaries.  All core computation is pure (no I/O inside
:func:`replay_events`); DB access is isolated to :func:`replay_db_range`.

Fixture mornings use the same JSON schema as
``tests/fixtures/wake/mornings/*.json``.  The :func:`load_fixture` helper
moved here from ``test_mornings.py`` so the CLI and tests share it.

Public API
----------
- :class:`FixtureData`    — loaded fixture morning (name, tz, events, …).
- :class:`ReplayEval`     — per-tick result from one replay pass.
- :class:`ReplayDay`      — aggregated per-day (baseline + candidate evals).
- :class:`DayComparison`  — comparison result for one day.
- :func:`load_fixture`    — load a fixture JSON file.
- :func:`replay_events`   — pure in-memory replay, threading PreviousTick.
- :func:`compare`         — derive latency / early-confirm metrics.
- :func:`replay_db_range` — DB-backed re-scoring of a date range.
- :func:`replay_fixture`  — fixture-backed re-scoring under two rule sets.
"""

from __future__ import annotations

import datetime
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import structlog
from sqlmodel import Session, select

from chronos.enums import DayState, EvaluationDecision
from chronos.models.wake import Day, RuleVersion, TelemetryEvent, WakeEvaluation
from chronos.wake.context import build_eval_context, make_previous_tick_from_row
from chronos.wake.rules import WakeRules, load_yaml
from chronos.wake.scoring import Evaluation, PreviousTick, evaluate
from chronos.wake.signals import Signals
from chronos.wake.window import local_date_for

log = structlog.get_logger(__name__)
_plain_log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class FixtureData:
    """Parsed fixture morning JSON file."""

    name: str
    tz: str
    day_kind: str  # "weekday" | "weekend" | "holiday"
    rules_file: str
    events: list[dict[str, Any]]
    expect_day: dict[str, Any] | None = None
    actual_wake_local: str | None = None  # HH:MM annotation (optional in fixture)


@dataclass(frozen=True)
class ReplayEval:
    """Per-tick result from one replay pass."""

    device_ts: datetime.datetime  # UTC-aware
    score: int
    decision: EvaluationDecision
    confirm_reason: Literal["strong", "consecutive", "both"] | None
    consecutive_high: int
    rule_version_id: int | None  # None for fixture replay
    wake_source: str


def _empty_replay_eval_list() -> list[ReplayEval]:
    """Typed factory for ReplayEval lists (used in dataclass fields)."""
    return []


@dataclass
class ReplayDay:
    """Aggregated per-day result: baseline and candidate evaluation lists."""

    local_date: datetime.date
    kind: str
    actual_wake_local: str | None  # HH:MM or None
    tz: str
    baseline_evals: list[ReplayEval] = field(default_factory=_empty_replay_eval_list)
    candidate_evals: list[ReplayEval] = field(default_factory=_empty_replay_eval_list)


@dataclass
class DayComparison:
    """Comparison result for one day (output of :func:`compare`)."""

    local_date: datetime.date
    kind: str
    actual_wake_local: str | None
    baseline_confirm_time: datetime.time | None  # local HH:MM of first confirm
    candidate_confirm_time: datetime.time | None
    latency_min: int | None  # candidate_confirm - actual_wake (minutes; may be negative)
    ticks_ge_confirm: int  # candidate ticks with score >= confirm threshold
    is_early: bool  # any confirm > early_min before actual_wake_local
    skipped_count: int = 0  # invalid raw_json rows skipped


# ---------------------------------------------------------------------------
# Fixture loader
# ---------------------------------------------------------------------------


def load_fixture(path: Path) -> FixtureData:
    """Load a fixture JSON file and return :class:`FixtureData`.

    Strips ``"_*"`` meta-keys and ``"expect"`` from each event dict so that
    the cleaned dicts can be passed directly to :func:`Signals.model_validate`.

    Args:
        path: Path to a fixture JSON file.

    Returns:
        A :class:`FixtureData` with ``events`` stripped of meta-keys.

    Raises:
        FileNotFoundError: If ``path`` does not exist.
        json.JSONDecodeError: If the file is not valid JSON.
    """
    raw: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    events: list[dict[str, Any]] = [
        {k: v for k, v in ev.items() if not k.startswith("_") and k != "expect"}
        for ev in raw.get("events", [])
    ]
    return FixtureData(
        name=raw.get("name", path.stem),
        tz=raw["tz"],
        day_kind=raw.get("day_kind", "weekday"),
        rules_file=raw.get("rules_file", "config/wake_rules.yaml"),
        events=events,
        expect_day=raw.get("expect_day"),
        actual_wake_local=raw.get("actual_wake_local"),
    )


# ---------------------------------------------------------------------------
# Pure replay core
# ---------------------------------------------------------------------------


def replay_events(
    events: list[dict[str, Any]],
    rules: WakeRules,
    rule_version_number: int,
    wake_source_key: str = "wake_source",
) -> list[ReplayEval]:
    """Pure in-memory replay, threading PreviousTick between events.

    Mirrors the loop in ``tests/wake/test_mornings.py`` exactly.  Events that
    fail :func:`~chronos.wake.signals.Signals.model_validate` are skipped with
    a warning (schema drift tolerance).

    Args:
        events:              List of signal dicts (meta-keys already stripped).
        rules:               Rule set to evaluate under.
        rule_version_number: Rule version number echoed in the EvalContext.
        wake_source_key:     Key to read the wake-source string from each event
                             (default: ``"wake_source"``).

    Returns:
        List of :class:`ReplayEval`, one per successfully parsed event.
    """
    window_cfg = rules.window.to_window_config()
    tz_obj: ZoneInfo | None = None  # derived from first valid event

    prev: PreviousTick | None = None
    ticks_today = 0
    confirmed_at_idx: int | None = None
    results: list[ReplayEval] = []

    for i, raw_event in enumerate(events):
        try:
            signals = Signals.model_validate(raw_event)
        except Exception as exc:
            _plain_log.warning(
                "replay_events_skip_invalid",
                extra={"event_index": i, "error": str(exc)},
            )
            continue

        # Derive timezone from first valid event.
        if tz_obj is None:
            tz_obj = ZoneInfo(signals.tz)

        # State machine: once confirmed, subsequent ticks use confirmed state.
        day_state = DayState.monitoring if confirmed_at_idx is None else DayState.confirmed

        ctx = build_eval_context(
            signals=signals,
            rules=rules,
            rule_version_number=rule_version_number,
            previous=prev,
            ticks_received_today=ticks_today,
            day_state=day_state,
        )

        # Use device_ts as "now" (same as test_mornings.py).
        evaluation: Evaluation = evaluate(signals, rules, signals.device_ts, ctx)

        wake_source = str(raw_event.get(wake_source_key, ""))
        results.append(
            ReplayEval(
                device_ts=signals.device_ts,
                score=evaluation.score,
                decision=evaluation.decision,
                confirm_reason=evaluation.confirm_reason,
                consecutive_high=evaluation.consecutive_high,
                rule_version_id=None,  # not from DB
                wake_source=wake_source,
            )
        )

        if evaluation.decision == EvaluationDecision.confirm and confirmed_at_idx is None:
            confirmed_at_idx = i

        # Build PreviousTick for next iteration.
        assert tz_obj is not None  # set above on first valid event
        device_ts_utc = signals.device_ts.astimezone(datetime.UTC)
        local_date = local_date_for(device_ts_utc, tz_obj, window_cfg.day_boundary)
        prev = PreviousTick(
            device_ts=signals.device_ts,
            local_date=local_date,
            battery_state=signals.battery_state,
            is_unlocked=signals.is_unlocked,
            score=evaluation.score,
            decision=evaluation.decision,
            consecutive_high=evaluation.consecutive_high,
        )
        ticks_today += 1

    return results


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def compare(
    baseline_evals: list[ReplayEval],
    candidate_evals: list[ReplayEval],
    actual_wake_local: str | None,
    early_min: int,
    confirm_threshold: int,
    tz: str,
) -> DayComparison:
    """Compare baseline and candidate evaluations for one day.

    Latency = ``candidate_confirm_local - actual_wake_local`` in minutes.
    Negative latency means the confirm fired *before* the actual wake time.

    ``is_early`` is True when any candidate confirm fires more than
    ``early_min`` minutes *before* ``actual_wake_local``.

    Args:
        baseline_evals:   Evaluations from the baseline (stored) rules.
        candidate_evals:  Evaluations from the candidate rules.
        actual_wake_local: Owner-annotated wake time as ``"HH:MM"``, or None.
        early_min:        Threshold in minutes for the early-confirm flag.
        confirm_threshold: Score threshold above which a tick counts as ≥ confirm.
        tz:               IANA timezone string for local-time conversion.

    Returns:
        A :class:`DayComparison` with latency and early-confirm metrics.
    """
    tz_obj = ZoneInfo(tz)

    def _first_confirm(evals: list[ReplayEval]) -> datetime.time | None:
        for ev in evals:
            if ev.decision == EvaluationDecision.confirm:
                return ev.device_ts.astimezone(tz_obj).time().replace(second=0, microsecond=0)
        return None

    baseline_confirm = _first_confirm(baseline_evals)
    candidate_confirm = _first_confirm(candidate_evals)

    ticks_ge_confirm = sum(1 for ev in candidate_evals if ev.score >= confirm_threshold)

    latency_min: int | None = None
    is_early = False

    if actual_wake_local is not None and candidate_confirm is not None:
        # Parse "HH:MM" into a time object.
        h, m = (int(x) for x in actual_wake_local.split(":"))
        actual_time = datetime.time(h, m)

        # Compute minutes delta (confirm - actual_wake): negative = early.
        confirm_total = candidate_confirm.hour * 60 + candidate_confirm.minute
        actual_total = actual_time.hour * 60 + actual_time.minute
        latency_min = confirm_total - actual_total

        # is_early: any candidate confirm > early_min minutes before actual wake.
        for ev in candidate_evals:
            if ev.decision == EvaluationDecision.confirm:
                ev_local = ev.device_ts.astimezone(tz_obj)
                ev_total = ev_local.hour * 60 + ev_local.minute
                if (actual_total - ev_total) > early_min:
                    is_early = True
                    break

    # Derive local_date from first eval.
    if candidate_evals:
        first_ts = candidate_evals[0].device_ts.astimezone(tz_obj)
        local_date = first_ts.date()
    elif baseline_evals:
        first_ts = baseline_evals[0].device_ts.astimezone(tz_obj)
        local_date = first_ts.date()
    else:
        local_date = datetime.date.today()

    return DayComparison(
        local_date=local_date,
        kind="",  # filled by caller who has the day row
        actual_wake_local=actual_wake_local,
        baseline_confirm_time=baseline_confirm,
        candidate_confirm_time=candidate_confirm,
        latency_min=latency_min,
        ticks_ge_confirm=ticks_ge_confirm,
        is_early=is_early,
    )


# ---------------------------------------------------------------------------
# DB-backed replay
# ---------------------------------------------------------------------------


def replay_db_range(
    session: Session,
    device_id: int,
    from_date: datetime.date,
    to_date: datetime.date,
    candidate_rules: WakeRules,
    candidate_rule_version_number: int,
) -> list[ReplayDay]:
    """Load telemetry events + stored evaluations for a date range; re-score.

    Baseline per evaluation uses the *stored* ``rule_version_id`` (loaded from
    the DB).  Invalid ``raw_json`` rows are skipped with a warning and counted.
    Only dates with at least one valid event are included in the result.

    Args:
        session:                       Open DB session (read-only in this call).
        device_id:                     Device primary-key ID.
        from_date:                     First local date (inclusive).
        to_date:                       Last local date (inclusive).
        candidate_rules:               Rules to use for the candidate re-score.
        candidate_rule_version_number: Version number for the candidate rules.

    Returns:
        List of :class:`ReplayDay`, one per local_date with valid events.
    """
    from_str = from_date.isoformat()
    to_str = to_date.isoformat()

    # Fetch all telemetry events in range, ordered by local_date + device_ts.
    te_stmt = (
        select(TelemetryEvent, WakeEvaluation)
        .join(  # type: ignore[call-overload]
            WakeEvaluation,
            WakeEvaluation.telemetry_event_id == TelemetryEvent.id,  # type: ignore[arg-type]
        )
        .where(
            TelemetryEvent.device_id == device_id,
            TelemetryEvent.local_date >= from_str,
            TelemetryEvent.local_date <= to_str,
        )
        .order_by(
            TelemetryEvent.local_date.asc(),  # type: ignore[attr-defined]
            TelemetryEvent.device_ts.asc(),  # type: ignore[attr-defined]
        )
    )
    rows = list(session.exec(te_stmt))  # type: ignore[call-overload]

    # Fetch day rows for actual_wake_local and kind.
    day_stmt = select(Day).where(
        Day.device_id == device_id,
        Day.local_date >= from_str,
        Day.local_date <= to_str,
    )
    day_rows: dict[str, Day] = {
        d.local_date: d
        for d in session.exec(day_stmt).all()  # type: ignore[call-overload]
    }

    # Cache rule version objects to avoid repeated DB lookups.
    rv_cache: dict[int, WakeRules | None] = {}

    def _get_baseline_rules(rv_id: int) -> WakeRules | None:
        if rv_id in rv_cache:
            return rv_cache[rv_id]
        rv_row = session.exec(  # type: ignore[call-overload]
            select(RuleVersion).where(RuleVersion.id == rv_id)
        ).first()
        if rv_row is None:
            rv_cache[rv_id] = None
            return None
        try:
            rules = WakeRules.model_validate_json(rv_row.rules_json)
        except Exception as exc:
            _plain_log.warning(
                "replay_db_bad_rules_json",
                extra={"rule_version_id": rv_id, "error": str(exc)},
            )
            rv_cache[rv_id] = None
            return None
        rv_cache[rv_id] = rules
        return rules

    # Group rows by local_date, process per-day.
    days_map: dict[str, ReplayDay] = {}
    skipped_total: dict[str, int] = {}

    # State per day for threading PreviousTick.
    prev_baseline: dict[str, PreviousTick | None] = {}
    prev_candidate: dict[str, PreviousTick | None] = {}
    ticks_today: dict[str, int] = {}
    confirmed_baseline: dict[str, bool] = {}
    confirmed_candidate: dict[str, bool] = {}

    for te, we in rows:
        local_date_str = te.local_date
        if local_date_str not in days_map:
            day_row = day_rows.get(local_date_str)
            days_map[local_date_str] = ReplayDay(
                local_date=datetime.date.fromisoformat(local_date_str),
                kind=day_row.kind if day_row else "weekday",
                actual_wake_local=day_row.actual_wake_local if day_row else None,
                tz=te.tz,
            )
            skipped_total[local_date_str] = 0
            prev_baseline[local_date_str] = None
            prev_candidate[local_date_str] = None
            ticks_today[local_date_str] = 0
            confirmed_baseline[local_date_str] = False
            confirmed_candidate[local_date_str] = False

        # Parse raw_json.
        try:
            signals = Signals.model_validate_json(te.raw_json)
        except Exception as exc:
            _plain_log.warning(
                "replay_db_skip_invalid_raw_json",
                extra={"telemetry_event_id": te.id, "error": str(exc)},
            )
            skipped_total[local_date_str] += 1
            continue

        replay_day = days_map[local_date_str]
        n_ticks = ticks_today[local_date_str]

        # ── Baseline re-score ──────────────────────────────────────────────
        baseline_rules = _get_baseline_rules(we.rule_version_id)
        if baseline_rules is not None:
            b_state = (
                DayState.confirmed if confirmed_baseline[local_date_str] else DayState.monitoring
            )
            b_ctx = build_eval_context(
                signals=signals,
                rules=baseline_rules,
                rule_version_number=we.rule_version_id,
                previous=prev_baseline[local_date_str],
                ticks_received_today=n_ticks,
                day_state=b_state,
            )
            b_eval = evaluate(signals, baseline_rules, signals.device_ts, b_ctx)
            replay_day.baseline_evals.append(
                ReplayEval(
                    device_ts=signals.device_ts,
                    score=b_eval.score,
                    decision=b_eval.decision,
                    confirm_reason=b_eval.confirm_reason,
                    consecutive_high=b_eval.consecutive_high,
                    rule_version_id=we.rule_version_id,
                    wake_source=te.wake_source,
                )
            )
            if b_eval.decision == EvaluationDecision.confirm:
                confirmed_baseline[local_date_str] = True
            # Update baseline previous tick.
            prev_baseline[local_date_str] = make_previous_tick_from_row(
                WakeEvaluation(
                    telemetry_event_id=te.id or 0,
                    rule_version_id=we.rule_version_id,
                    local_date=local_date_str,
                    gate=b_eval.gate.value,
                    score=b_eval.score,
                    breakdown_json="",
                    decision=b_eval.decision.value,
                    consecutive_high=b_eval.consecutive_high,
                    evaluated_at="",
                ),
                te,
            )

        # ── Candidate re-score ─────────────────────────────────────────────
        c_state = DayState.confirmed if confirmed_candidate[local_date_str] else DayState.monitoring
        c_ctx = build_eval_context(
            signals=signals,
            rules=candidate_rules,
            rule_version_number=candidate_rule_version_number,
            previous=prev_candidate[local_date_str],
            ticks_received_today=n_ticks,
            day_state=c_state,
        )
        c_eval = evaluate(signals, candidate_rules, signals.device_ts, c_ctx)
        replay_day.candidate_evals.append(
            ReplayEval(
                device_ts=signals.device_ts,
                score=c_eval.score,
                decision=c_eval.decision,
                confirm_reason=c_eval.confirm_reason,
                consecutive_high=c_eval.consecutive_high,
                rule_version_id=None,
                wake_source=te.wake_source,
            )
        )
        if c_eval.decision == EvaluationDecision.confirm:
            confirmed_candidate[local_date_str] = True
        # Update candidate previous tick.
        prev_candidate[local_date_str] = make_previous_tick_from_row(
            WakeEvaluation(
                telemetry_event_id=te.id or 0,
                rule_version_id=candidate_rule_version_number,
                local_date=local_date_str,
                gate=c_eval.gate.value,
                score=c_eval.score,
                breakdown_json="",
                decision=c_eval.decision.value,
                consecutive_high=c_eval.consecutive_high,
                evaluated_at="",
            ),
            te,
        )

        ticks_today[local_date_str] += 1

    return list(days_map.values())


# ---------------------------------------------------------------------------
# Fixture replay
# ---------------------------------------------------------------------------


def replay_fixture(
    fixture: FixtureData,
    baseline_rules: WakeRules,
    candidate_rules: WakeRules,
    baseline_version: int = 1,
    candidate_version: int = 2,
) -> ReplayDay:
    """Replay one fixture under baseline and candidate rules (no DB access).

    Args:
        fixture:            Loaded :class:`FixtureData`.
        baseline_rules:     Rule set representing the baseline.
        candidate_rules:    Rule set representing the candidate.
        baseline_version:   Rule version number for the baseline (default 1).
        candidate_version:  Rule version number for the candidate (default 2).

    Returns:
        A :class:`ReplayDay` with baseline and candidate evals.
    """
    # Derive local_date from the first event's device_ts.
    local_date = datetime.date.today()
    if fixture.events:
        try:
            sig0 = Signals.model_validate(fixture.events[0])
            tz_obj = ZoneInfo(fixture.tz)
            window_cfg = baseline_rules.window.to_window_config()
            local_date = local_date_for(sig0.device_ts, tz_obj, window_cfg.day_boundary)
        except Exception as exc:
            _plain_log.debug("replay_fixture_local_date_fallback", exc_info=exc)

    baseline_evals = replay_events(fixture.events, baseline_rules, baseline_version)
    candidate_evals = replay_events(fixture.events, candidate_rules, candidate_version)

    return ReplayDay(
        local_date=local_date,
        kind=fixture.day_kind,
        actual_wake_local=fixture.actual_wake_local,
        tz=fixture.tz,
        baseline_evals=baseline_evals,
        candidate_evals=candidate_evals,
    )


# ---------------------------------------------------------------------------
# Comparison summary helpers
# ---------------------------------------------------------------------------


def build_comparisons(
    replay_days: list[ReplayDay],
    candidate_rules: WakeRules,
    early_min: int = 30,
) -> list[DayComparison]:
    """Convert a list of :class:`ReplayDay` into :class:`DayComparison` objects.

    Args:
        replay_days:    Output of :func:`replay_db_range` or a list of
                        :func:`replay_fixture` results.
        candidate_rules: Used to extract the confirm threshold.
        early_min:       Early-confirm flag threshold in minutes (default 30).

    Returns:
        List of :class:`DayComparison`, one per day.
    """
    confirm_threshold = candidate_rules.thresholds.confirm
    comparisons: list[DayComparison] = []
    for day in replay_days:
        cmp = compare(
            baseline_evals=day.baseline_evals,
            candidate_evals=day.candidate_evals,
            actual_wake_local=day.actual_wake_local,
            early_min=early_min,
            confirm_threshold=confirm_threshold,
            tz=day.tz,
        )
        cmp.kind = day.kind  # fill in kind from the day row
        comparisons.append(cmp)
    return comparisons


def summary_line(
    comparisons: list[DayComparison],
    target_latency: int = 10,
) -> str:
    """Build a one-line G1 summary across all days.

    Args:
        comparisons:     Output of :func:`build_comparisons`.
        target_latency:  G1 median latency target in minutes (default 10).

    Returns:
        A human-readable summary string.
    """
    n = len(comparisons)
    # Candidate latencies (baseline latency not stored separately).
    c_latencies: list[int] = [c.latency_min for c in comparisons if c.latency_min is not None]

    def _median(vals: list[int]) -> int | None:
        if not vals:
            return None
        s = sorted(vals)
        mid = len(s) // 2
        return s[mid]

    c_med = _median(c_latencies)
    early = sum(1 for c in comparisons if c.is_early)
    g1_pass = c_med is not None and c_med <= target_latency and early == 0

    med_str = f"{c_med} min" if c_med is not None else "n/a"
    pass_str = "PASS" if g1_pass else "FAIL"
    return (
        f"{n} days replayed | "
        f"median latency {med_str} (target ≤ {target_latency}) · "
        f"early confirms {early} (target 0) → {pass_str}"
    )


def load_rules_for_version(
    session: Session,
    rule_version_id: int,
) -> WakeRules | None:
    """Load a :class:`WakeRules` from the DB by rule_version PK.

    Returns None if not found or if the stored JSON is invalid.
    """
    rv_row = session.exec(  # type: ignore[call-overload]
        select(RuleVersion).where(RuleVersion.id == rule_version_id)
    ).first()
    if rv_row is None:
        return None
    try:
        return WakeRules.model_validate_json(rv_row.rules_json)
    except Exception:
        return None


def candidate_rules_from_path_or_version(
    session: Session,
    rules_path: Path | None,
    rule_version: int | None,
) -> tuple[WakeRules, int]:
    """Resolve candidate rules from a path, a version number, or the active version.

    Returns:
        (WakeRules, version_number) for the candidate.
    """
    if rules_path is not None:
        rules = load_yaml(rules_path)
        return rules, 0  # 0 = "from file, no DB version"

    if rule_version is not None:
        rv_row = session.exec(  # type: ignore[call-overload]
            select(RuleVersion).where(RuleVersion.version == rule_version)
        ).first()
        if rv_row is None:
            raise ValueError(f"Rule version {rule_version} not found in DB")
        rules = WakeRules.model_validate_json(rv_row.rules_json)
        assert rv_row.id is not None
        return rules, rv_row.id

    # Default: active version.
    from chronos.wake.rules import get_rules_cache

    rules, active_rv = get_rules_cache().get_effective(session)
    return rules, active_rv.version
