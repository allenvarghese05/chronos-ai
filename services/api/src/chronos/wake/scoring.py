"""Pure wake-engine scoring function (T1.5).

``evaluate(signals, rules, now, ctx) -> Evaluation`` is the heart of
Chronos-AI.  It is deliberately free of I/O, database access and
:class:`~chronos.config.Settings` imports so that:

- It can be called in unit tests with a fixed clock.
- Every result is reproducible given the same inputs.
- The caller threads state via :class:`EvalContext` / :class:`PreviousTick`.

Algorithm (in order):
    1. Compute ``local_date`` from ``signals.tz`` + day boundary.
    2. Validate ``ctx.previous``: cross-day or out-of-order → treat as None.
    3. Gate check: device_ts outside ``[window.start_utc, window.end_utc)``
       → score 0, ``outside_window`` decision, return immediately.
    4. Derive: ``charger_disconnected``, ``consecutive_unlocked_ticks``,
       ``minutes_since_window_start``, ``pedometer_unavailable``.
    5. Dedupe check: gap to previous < ``dedupe_min`` minutes → deduped flag.
    6. Evaluate rules (YAML order): skip pedometer rules if unavailable;
       check ``requires``; find first-satisfied points row; apply
       ``bonus_consecutive``; apply effective cap.
    7. Apply penalties (AND-ed ``when`` conditions).
    8. Clamp score to [0, 100].
    9. Compute ``consecutive_high`` (deduped ticks do NOT advance).
    10. Determine ``decision`` and ``confirm_reason`` per precedence.

Public API
----------
- :class:`PreviousTick` — snapshot of the last evaluated tick.
- :class:`EvalContext` — server-side context for one evaluation call.
- :class:`DerivedFacts` — server-derived facts emitted for debugging.
- :class:`RuleBreakdown` — per-rule contribution to the score.
- :class:`PenaltyBreakdown` — per-penalty contribution.
- :class:`ThresholdsEcho` — active threshold values echoed in the response.
- :class:`Evaluation` — complete result returned by :func:`evaluate`.
- :func:`evaluate` — pure scoring function.
"""

from __future__ import annotations

import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict

from chronos.enums import (
    BatteryState,
    DayState,
    EvaluationDecision,
    EvaluationGate,
    PedometerAuth,
)
from chronos.wake.conditions import parse_condition
from chronos.wake.rules import WakeRules
from chronos.wake.signals import SIGNALS, SignalKind, Signals
from chronos.wake.window import Window, is_in_window, local_date_for

# ---------------------------------------------------------------------------
# Supporting types
# ---------------------------------------------------------------------------


class PreviousTick(BaseModel):
    """Snapshot of the last evaluated tick (same local_date, in order).

    Passed inside :class:`EvalContext` so that ``evaluate`` can compute
    derived facts (charger transition, consecutive unlocked, gap to previous).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    device_ts: datetime.datetime
    """UTC-aware timestamp of the previous tick."""

    local_date: datetime.date
    """Local calendar date of the previous tick."""

    battery_state: BatteryState
    """Battery/charger state at the previous tick."""

    is_unlocked: bool
    """Whether the phone was unlocked at the previous tick."""

    score: int
    """0-100 score from the previous evaluation."""

    decision: EvaluationDecision
    """Decision reached at the previous evaluation."""

    consecutive_high: int
    """Consecutive-high counter after the previous evaluation (≥ 0)."""


class EvalContext(BaseModel):
    """Server-side context passed into :func:`evaluate`.

    The caller is responsible for threading state between ticks:
    ``previous`` must be the :class:`PreviousTick` built from the last
    *non-deduplicated, non-outside-window* evaluation, or ``None`` for the
    first tick of the day.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    window: Window
    """Resolved window for today's local date."""

    day_state: DayState
    """Current state-machine state for today's day row."""

    rule_version: int
    """Version of the active rule set (echoed in :class:`Evaluation`)."""

    previous: PreviousTick | None = None
    """Last tick snapshot, or ``None`` if this is the first tick today."""

    ticks_received_today: int = 0
    """Total ticks evaluated today (including deduped ones)."""


class DerivedFacts(BaseModel):
    """Server-derived facts computed inside :func:`evaluate`; emitted for debugging.

    These are **not** passed in externally — they are computed from
    ``ctx.previous`` + ``signals`` inside ``evaluate``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    charger_disconnected: bool
    """True iff previous battery was charging/full and current is unplugged."""

    consecutive_unlocked_ticks: int
    """0 if currently locked; 1 if unlocked without a prior unlocked tick; ≥ 2 if both."""

    minutes_since_window_start: int
    """Minutes since ``window.start_utc``, clamped to 0 if device_ts < start."""

    ticks_received_today: int
    """Copy of ``ctx.ticks_received_today`` for the response."""

    pedometer_unavailable: bool
    """True when ``pedometer_auth`` is denied, restricted, or not_determined."""


class RuleBreakdown(BaseModel):
    """Per-rule contribution to the score."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rule_id: str
    """Matches :attr:`~chronos.wake.rules.Rule.id`."""

    input: int | float | bool | str
    """The signal value used for this rule."""

    points: int
    """Points awarded (0 if skipped, requires unmet, or threshold not reached)."""

    cap: int
    """Effective cap applied (may differ from rule.cap when reweighted)."""

    note: str | None = None
    """Human-readable explanation (skipped, bonus applied, requires unmet, etc.)."""


class PenaltyBreakdown(BaseModel):
    """Per-penalty contribution."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rule_id: str
    """``"penalty:<id>"`` form."""

    points: int
    """≤ 0 (0 = mark-only penalty that fired)."""

    note: str | None = None
    """Human-readable explanation of which conditions fired."""


class ThresholdsEcho(BaseModel):
    """Echo of active thresholds for the API response."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    confirm: int
    strong: int
    confirm_max_gap_min: int


class Evaluation(BaseModel):
    """Complete scoring result returned by :func:`evaluate`."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    gate: EvaluationGate
    """Whether the evaluation window was open."""

    score: int
    """Clamped score in [0, 100]."""

    decision: EvaluationDecision
    """Wake decision (see :class:`~chronos.enums.EvaluationDecision`)."""

    confirm_reason: Literal["strong", "consecutive", "both"] | None = None
    """Why a ``confirm`` decision was reached; ``None`` for non-confirm decisions."""

    consecutive_high: int
    """Number of consecutive high-scoring ticks (0 when not high or deduped)."""

    thresholds: ThresholdsEcho
    """Active threshold values echoed for the API response."""

    rule_version: int
    """Version of the active rule set."""

    breakdown: tuple[RuleBreakdown, ...]
    """Rule contributions in YAML order."""

    penalties: tuple[PenaltyBreakdown, ...]
    """Penalty contributions (only fired penalties are included)."""

    derived: DerivedFacts
    """Server-derived facts for debugging / replay."""

    notes: tuple[str, ...] = ()
    """Additional notes (e.g., previous-tick anomalies, reweighting notices)."""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

_PEDOMETER_UNAVAILABLE: frozenset[PedometerAuth] = frozenset(
    {PedometerAuth.denied, PedometerAuth.restricted, PedometerAuth.not_determined}
)

_CHARGING_STATES: frozenset[BatteryState] = frozenset({BatteryState.charging, BatteryState.full})


def _signal_values(
    signals: Signals,
    derived: DerivedFacts,
) -> dict[str, int | float | bool | str]:
    """Build the flat signal-value dict used during rule evaluation."""
    return {
        "device_ts": signals.device_ts.isoformat(),
        "tz": signals.tz,
        "wake_source": signals.wake_source.value,
        "steps_10m": signals.steps_10m,
        "steps_since_window": signals.steps_since_window,
        "walking_sec_10m": signals.walking_sec_10m,
        "walking_conf": signals.walking_conf.value,
        "stationary_sec_10m": signals.stationary_sec_10m,
        "is_unlocked": signals.is_unlocked,
        "battery_state": signals.battery_state.value,
        "battery_level": signals.battery_level if signals.battery_level is not None else 0.0,
        "app_state": signals.app_state.value,
        "low_power_mode": signals.low_power_mode,
        "pedometer_auth": signals.pedometer_auth.value,
        "seq": signals.seq,
        # derived
        "charger_disconnected": derived.charger_disconnected,
        "consecutive_unlocked_ticks": derived.consecutive_unlocked_ticks,
        "minutes_since_window_start": derived.minutes_since_window_start,
        "ticks_received_today": derived.ticks_received_today,
    }


# ---------------------------------------------------------------------------
# Public function
# ---------------------------------------------------------------------------


def evaluate(
    signals: Signals,
    rules: WakeRules,
    now: datetime.datetime,
    ctx: EvalContext,
) -> Evaluation:
    """Score telemetry signals and return a wake decision (pure — no I/O).

    Args:
        signals: Validated phone telemetry snapshot.
        rules:   Active rule set (loaded from YAML or DB).
        now:     Current UTC time (injected by the caller; not read internally).
        ctx:     Server-side context including window, day state, and previous tick.

    Returns:
        A frozen :class:`Evaluation` describing the score, decision, and
        per-rule breakdown.

    Notes:
        ``now`` is accepted for API symmetry and future audit logging;
        the function does not use it to drive any branch logic — all
        time comparisons use ``signals.device_ts`` and ``ctx.window``.
    """
    thresholds_echo = ThresholdsEcho(
        confirm=rules.thresholds.confirm,
        strong=rules.thresholds.strong,
        confirm_max_gap_min=rules.thresholds.confirm_max_gap_min,
    )

    # ── 1. Compute local_date ───────────────────────────────────────────────
    tz = ZoneInfo(signals.tz)
    window_cfg = ctx.window  # Window already computed by caller
    boundary = rules.window.to_window_config().day_boundary
    local_date = local_date_for(signals.device_ts, tz, boundary)

    # ── 2. Validate previous ───────────────────────────────────────────────
    notes: list[str] = []
    prev = ctx.previous
    if prev is not None:
        if prev.local_date != local_date:
            notes.append(
                f"previous from {prev.local_date} (current={local_date}), treating as None"
            )
            prev = None
        elif prev.device_ts >= signals.device_ts:
            notes.append(
                f"out-of-order previous device_ts ({prev.device_ts} >= {signals.device_ts}), "
                "treating as None"
            )
            prev = None

    # ── 3. Gate check ──────────────────────────────────────────────────────
    in_window = is_in_window(signals.device_ts, window_cfg)
    gate = EvaluationGate.open if in_window else EvaluationGate.closed

    # Shared "closed-window" return used twice (here and in already_confirmed path).
    def _closed_return(decision: EvaluationDecision) -> Evaluation:
        empty_derived = DerivedFacts(
            charger_disconnected=False,
            consecutive_unlocked_ticks=0,
            minutes_since_window_start=0,
            ticks_received_today=ctx.ticks_received_today,
            pedometer_unavailable=signals.pedometer_auth in _PEDOMETER_UNAVAILABLE,
        )
        return Evaluation(
            gate=gate,
            score=0,
            decision=decision,
            confirm_reason=None,
            consecutive_high=0,
            thresholds=thresholds_echo,
            rule_version=ctx.rule_version,
            breakdown=(),
            penalties=(),
            derived=empty_derived,
            notes=tuple(notes),
        )

    if gate == EvaluationGate.closed:
        return _closed_return(EvaluationDecision.outside_window)

    # ── 4. Derived facts ───────────────────────────────────────────────────
    charger_disconnected = (
        prev is not None
        and prev.battery_state in _CHARGING_STATES
        and signals.battery_state == BatteryState.unplugged
    )

    if not signals.is_unlocked:
        consecutive_unlocked_ticks = 0
    elif prev is not None and prev.is_unlocked:
        consecutive_unlocked_ticks = 2
    else:
        consecutive_unlocked_ticks = 1

    minutes_since_window_start = max(
        0,
        int((signals.device_ts - window_cfg.start_utc).total_seconds() / 60),
    )

    pedometer_unavailable = signals.pedometer_auth in _PEDOMETER_UNAVAILABLE

    derived = DerivedFacts(
        charger_disconnected=charger_disconnected,
        consecutive_unlocked_ticks=consecutive_unlocked_ticks,
        minutes_since_window_start=minutes_since_window_start,
        ticks_received_today=ctx.ticks_received_today,
        pedometer_unavailable=pedometer_unavailable,
    )

    sig_vals = _signal_values(signals, derived)

    # ── 5. Dedupe check ────────────────────────────────────────────────────
    deduped = False
    if prev is not None:
        gap_s = (signals.device_ts - prev.device_ts).total_seconds()
        if gap_s < rules.thresholds.dedupe_min * 60:
            deduped = True

    # ── 6. Already confirmed ───────────────────────────────────────────────
    # Check before rule evaluation so we don't waste cycles.
    already_conf = ctx.day_state in {DayState.confirmed, DayState.delivered}

    # ── 7. Rule evaluation ─────────────────────────────────────────────────
    breakdown_list: list[RuleBreakdown] = []
    raw_score = 0

    for rule in rules.rules:
        spec = SIGNALS[rule.signal]

        # 7a. Skip pedometer-requiring rules when unavailable.
        if pedometer_unavailable and spec.needs_pedometer:
            breakdown_list.append(
                RuleBreakdown(
                    rule_id=rule.id,
                    input=sig_vals.get(rule.signal, 0),
                    points=0,
                    cap=rule.cap,
                    note="pedometer unavailable",
                )
            )
            continue

        # 7b. Requires check (ALL conditions must pass).
        requires_failed = False
        requires_note: str | None = None
        if rule.requires:
            for req_sig, req_vals in rule.requires.items():
                actual = str(sig_vals[req_sig])
                allowed = [str(v) for v in req_vals]
                if actual not in allowed:
                    requires_failed = True
                    requires_note = f"requires {req_sig} in {allowed!r}, got {actual!r}"
                    break

        if requires_failed:
            breakdown_list.append(
                RuleBreakdown(
                    rule_id=rule.id,
                    input=sig_vals.get(rule.signal, 0),
                    points=0,
                    cap=rule.cap,
                    note=requires_note,
                )
            )
            continue

        # 7c. Find first satisfied threshold row.
        sig_val = sig_vals[rule.signal]
        pts = 0
        for row in rule.points:
            threshold = row[0]
            award = row[1]
            if spec.kind is SignalKind.numeric:
                if isinstance(sig_val, (int, float)) and isinstance(threshold, (int, float)):
                    if sig_val >= threshold:
                        pts = int(award)
                        break
            else:
                # bool / enum: string comparison (case-insensitive)
                if str(sig_val).lower() == str(threshold).lower():
                    pts = int(award)
                    break

        # 7d. Effective cap + reweighted base points (Owner Answer 17).
        #
        # A ``reweight_without_pedometer`` value sets **both** the cap and
        # the effective points for a rule:
        #
        # - bool / enum rules (no bonus_consecutive): the single row's pts
        #   become the reweight value.
        # - rules with bonus_consecutive: base row pts = reweight_value -
        #   bonus.points (e.g., unlocked 35 → base 25 + bonus 10).
        # - numeric rules: proportional scaling so the top row = reweight
        #   value (none listed today; document only).
        #
        # The validator ensures reweight_value > bonus.points for bonus rules.
        reweight_value: int | None = (
            rules.reweight_without_pedometer.get(rule.id) if pedometer_unavailable else None
        )
        effective_cap = reweight_value if reweight_value is not None else rule.cap

        reweight_note: str | None = None
        if reweight_value is not None and pts > 0:
            if rule.bonus_consecutive:
                # base row = reweight_value - bonus.points
                base_pts = reweight_value - rule.bonus_consecutive.points
                pts = base_pts
                reweight_note = f"reweighted: base {base_pts} (pedometer unavailable)"
            else:
                # bool / enum single-row: pts = reweight_value
                pts = reweight_value
                reweight_note = f"reweighted: pts={reweight_value} (pedometer unavailable)"

        # 7e. Bonus consecutive (only for unlocked rule with gap ≤ max).
        bonus_note: str | None = None
        if rule.bonus_consecutive and not deduped and pts > 0:
            if prev is not None and prev.is_unlocked and signals.is_unlocked:
                gap_min = (signals.device_ts - prev.device_ts).total_seconds() / 60
                if gap_min <= rules.thresholds.confirm_max_gap_min:
                    bonus = rule.bonus_consecutive.points
                    if reweight_value is not None:
                        # With bonus the total reaches the full reweight value.
                        pts = reweight_value
                    else:
                        pts = min(pts + bonus, effective_cap)
                    bonus_note = f"consecutive bonus +{bonus}"
                else:
                    pts = min(pts, effective_cap)
            else:
                pts = min(pts, effective_cap)
        else:
            pts = min(pts, effective_cap)

        note_parts = [p for p in (reweight_note, bonus_note) if p]
        note: str | None = "; ".join(note_parts) if note_parts else None
        breakdown_list.append(
            RuleBreakdown(
                rule_id=rule.id,
                input=sig_val,  # type: ignore[arg-type]
                points=pts,
                cap=effective_cap,
                note=note,
            )
        )
        raw_score += pts

    # ── 8. Penalties (AND-ed when conditions) ──────────────────────────────
    penalty_list: list[PenaltyBreakdown] = []

    for penalty in rules.penalties:
        all_met = True
        fired_conditions: list[str] = []
        for sig_name, raw_cond in penalty.when.items():
            val = sig_vals[sig_name]
            cond = parse_condition(raw_cond)  # type: ignore[arg-type]
            if not cond.evaluate(val):  # type: ignore[arg-type]
                all_met = False
                break
            fired_conditions.append(f"{sig_name} {raw_cond}")

        if all_met:
            penalty_note = "; ".join(fired_conditions) if fired_conditions else None
            penalty_list.append(
                PenaltyBreakdown(
                    rule_id=f"penalty:{penalty.id}",
                    points=penalty.points,
                    note=penalty_note,
                )
            )
            raw_score += penalty.points

    # ── 9. Clamp ───────────────────────────────────────────────────────────
    score = max(0, min(100, raw_score))

    # ── 10. Consecutive high ───────────────────────────────────────────────
    if deduped:
        consecutive_high = prev.consecutive_high if prev is not None else 0
    elif score >= rules.thresholds.confirm:
        gap_ok = (
            prev is not None
            and (signals.device_ts - prev.device_ts).total_seconds() / 60
            <= rules.thresholds.confirm_max_gap_min
        )
        if prev is not None and prev.consecutive_high >= 1 and gap_ok:
            consecutive_high = prev.consecutive_high + 1
        else:
            consecutive_high = 1
    else:
        consecutive_high = 0

    # ── 11. Decision + confirm_reason ──────────────────────────────────────
    if already_conf:
        return Evaluation(
            gate=gate,
            score=score,
            decision=EvaluationDecision.already_confirmed,
            confirm_reason=None,
            consecutive_high=consecutive_high,
            thresholds=thresholds_echo,
            rule_version=ctx.rule_version,
            breakdown=tuple(breakdown_list),
            penalties=tuple(penalty_list),
            derived=derived,
            notes=tuple(notes),
        )

    if deduped:
        return Evaluation(
            gate=gate,
            score=score,
            decision=EvaluationDecision.ignored_dedupe,
            confirm_reason=None,
            consecutive_high=consecutive_high,
            thresholds=thresholds_echo,
            rule_version=ctx.rule_version,
            breakdown=tuple(breakdown_list),
            penalties=tuple(penalty_list),
            derived=derived,
            notes=tuple(notes),
        )

    strong = score >= rules.thresholds.strong
    consec = consecutive_high >= 2

    if strong or consec:
        decision = EvaluationDecision.confirm
        if strong and consec:
            confirm_reason: Literal["strong", "consecutive", "both"] | None = "both"
        elif strong:
            confirm_reason = "strong"
        else:
            confirm_reason = "consecutive"
    elif score >= rules.thresholds.confirm:
        decision = EvaluationDecision.high
        confirm_reason = None
    else:
        decision = EvaluationDecision.low
        confirm_reason = None

    return Evaluation(
        gate=gate,
        score=score,
        decision=decision,
        confirm_reason=confirm_reason,
        consecutive_high=consecutive_high,
        thresholds=thresholds_echo,
        rule_version=ctx.rule_version,
        breakdown=tuple(breakdown_list),
        penalties=tuple(penalty_list),
        derived=derived,
        notes=tuple(notes),
    )
