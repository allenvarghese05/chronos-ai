"""Nightly Markdown report builder for Chronos-AI (T1.8).

Builds a human-readable Markdown audit trail of one device/day: score
timeline, state-machine transitions, push-notification summary, and anomaly
flags.  The report is produced from the DB — no I/O inside the scoring path.

Public API
----------
- :func:`build_day_report`  — build a Markdown string from DB data.
- :func:`write_day_report`  — write the string to ``reports_dir/{date}.md``.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import structlog
from sqlmodel import Session, select

from chronos.enums import EvaluationDecision, EvaluationGate, PushKind
from chronos.models.delivery import PushNotification
from chronos.models.wake import Day, DayTransition, RuleVersion, TelemetryEvent, WakeEvaluation
from chronos.report.sparkline import sparkline

log = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _local_time(ts_iso: str, tz: ZoneInfo) -> str:
    """Convert a UTC ISO-8601 string to local ``HH:MM`` format."""
    try:
        dt = datetime.datetime.fromisoformat(ts_iso.rstrip("Z"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.UTC)
        return dt.astimezone(tz).strftime("%H:%M")
    except ValueError:
        return ts_iso


def _tz_abbr(tz: ZoneInfo, dt: datetime.datetime) -> str:
    """Return the timezone abbreviation for *dt* in *tz*."""
    local = dt.astimezone(tz)
    return local.strftime("%Z")


def _parse_hhmm(s: str) -> datetime.time | None:
    """Parse ``HH:MM`` string to :class:`datetime.time`, or None on error."""
    try:
        h, m = (int(x) for x in s.split(":"))
        return datetime.time(h, m)
    except (ValueError, AttributeError):
        return None


def _latency_str(confirmed_local: str | None, actual_wake: str | None) -> str:
    """Compute latency in minutes between confirm and actual wake."""
    if confirmed_local is None or actual_wake is None:
        return "n/a"
    ct = _parse_hhmm(confirmed_local)
    at = _parse_hhmm(actual_wake)
    if ct is None or at is None:
        return "n/a"
    delta = (ct.hour * 60 + ct.minute) - (at.hour * 60 + at.minute)
    sign = "+" if delta >= 0 else ""
    return f"{sign}{delta} min"


# ---------------------------------------------------------------------------
# Main builder
# ---------------------------------------------------------------------------


def build_day_report(
    session: Session,
    device_id: int,
    local_date: datetime.date,
    now: datetime.datetime,
) -> str:
    """Build a Markdown report for one device/day.

    Sections: header, state/confirm/fallback, actual wake + latency,
    counts (ticks_sent, ticks_received, evaluations, deduped, outside_window),
    score timeline table (local device_ts, source, score, decision, reason),
    sparkline (fixed 0-100), state transitions, push-notification summary,
    rule version, and anomaly notes.

    Args:
        session:     Open DB session (read-only).
        device_id:   Device PK.
        local_date:  The local calendar date to report on.
        now:         UTC now (for the "Report generated" timestamp).

    Returns:
        Markdown string (empty-state report if no evaluations exist).

    Raises:
        ValueError: If no ``days`` row exists for (device_id, local_date).
    """
    local_date_str = local_date.isoformat()

    # ── Fetch day row ──────────────────────────────────────────────────────
    day = session.exec(  # type: ignore[call-overload]
        select(Day).where(Day.local_date == local_date_str, Day.device_id == device_id)
    ).first()
    if day is None:
        raise ValueError(f"No day row for device={device_id} local_date={local_date_str}")

    tz = ZoneInfo(day.tz)
    abbr = _tz_abbr(tz, now)

    # ── Fetch evaluations ──────────────────────────────────────────────────
    eval_rows = list(
        session.exec(  # type: ignore[call-overload]
            select(WakeEvaluation, TelemetryEvent)
            .join(  # type: ignore[call-overload]
                TelemetryEvent,
                WakeEvaluation.telemetry_event_id == TelemetryEvent.id,  # type: ignore[arg-type]
            )
            .where(
                TelemetryEvent.device_id == device_id,
                TelemetryEvent.local_date == local_date_str,
            )
            .order_by(TelemetryEvent.device_ts.asc())  # type: ignore[attr-defined]
        ).all()
    )

    total_evals = len(eval_rows)
    deduped = sum(
        1 for we, _ in eval_rows if we.decision == EvaluationDecision.ignored_dedupe.value
    )
    outside_window = sum(1 for we, _ in eval_rows if we.gate == EvaluationGate.closed.value)

    # ── Fetch transitions ─────────────────────────────────────────────────
    transitions = list(
        session.exec(  # type: ignore[call-overload]
            select(DayTransition)
            .where(DayTransition.local_date == local_date_str, DayTransition.device_id == device_id)
            .order_by(DayTransition.at.asc())  # type: ignore[attr-defined]
        ).all()
    )

    # ── Fetch push notifications ──────────────────────────────────────────
    push_rows = list(
        session.exec(  # type: ignore[call-overload]
            select(PushNotification).where(
                PushNotification.device_id == device_id,
                PushNotification.local_date == local_date_str,
            )
        ).all()
    )

    tick_pushes = [p for p in push_rows if p.kind == PushKind.tick.value]
    push_errors: dict[str, int] = {}
    for p in push_rows:
        if p.error:
            push_errors[p.error] = push_errors.get(p.error, 0) + 1

    # ── Fetch rule version ────────────────────────────────────────────────
    rule_version_id: int | None = None
    rule_version_num: str = "unknown"
    rule_version_sha: str = ""
    for we, _ in eval_rows:
        if we.rule_version_id:
            rule_version_id = we.rule_version_id
            break
    if rule_version_id is not None:
        rv_row = session.exec(  # type: ignore[call-overload]
            select(RuleVersion).where(RuleVersion.id == rule_version_id)
        ).first()
        if rv_row is not None:
            rule_version_num = str(rv_row.version)
            rule_version_sha = rv_row.sha256[:12]

    # ── Window strings ────────────────────────────────────────────────────
    window_start_local = _local_time(day.window_start_utc, tz)
    window_end_local = _local_time(day.window_end_utc, tz)

    # ── Day header ────────────────────────────────────────────────────────
    try:
        day_dt = datetime.datetime(local_date.year, local_date.month, local_date.day, tzinfo=tz)
        day_label = day_dt.strftime("%A, %-d %b %Y")
    except ValueError:
        day_label = local_date_str

    lines: list[str] = [
        f"# Morning Report - {day_label}",
        "",
        f"**Date:** {local_date_str}",
        f"**Kind:** {day.kind}",
        f"**Timezone:** {day.tz}",
        f"**Window:** {window_start_local}-{window_end_local} {abbr}",
        "",
    ]

    # ── State section ─────────────────────────────────────────────────────
    lines += ["## State", ""]
    lines += [f"**Status:** {day.state}"]

    confirmed_local_hhmm: str | None = None
    if day.confirmed_at:
        confirmed_local_hhmm = _local_time(day.confirmed_at, tz)
        lines.append(f"**Confirmed at:** {confirmed_local_hhmm} {abbr}")

        # Find the confirm evaluation.
        if day.confirm_evaluation_id is not None:
            conf_we = session.exec(  # type: ignore[call-overload]
                select(WakeEvaluation).where(WakeEvaluation.id == day.confirm_evaluation_id)
            ).first()
            if conf_we is not None:
                reason_str = ""
                try:
                    import json as _json

                    bd = _json.loads(conf_we.breakdown_json)
                    cr = bd.get("confirm_reason")
                    if cr:
                        reason_str = f", reason: {cr}"
                except Exception as exc:
                    log.debug("report_breakdown_parse_error", exc_info=exc)
                lines.append(
                    f"**Confirmed by:** evaluation #{conf_we.id} "
                    f"(score {conf_we.score}{reason_str})"
                )

    if day.actual_wake_local:
        lines.append(f"**Actual wake:** {day.actual_wake_local} {abbr} (manual annotation)")
        latency = _latency_str(confirmed_local_hhmm, day.actual_wake_local)
        lines.append(f"**Latency:** {latency} from actual")

        # G1 line.
        if confirmed_local_hhmm:
            at_t = _parse_hhmm(day.actual_wake_local)
            ct_t = _parse_hhmm(confirmed_local_hhmm)
            if at_t and ct_t:
                lat = (ct_t.hour * 60 + ct_t.minute) - (at_t.hour * 60 + at_t.minute)
                g1_pass = lat <= 10
                sign = "+" if lat >= 0 else ""
                lines.append(
                    f"**G1:** median latency {sign}{lat} min (target <= 10) · "
                    f"early confirms n/a (target 0) -> {'PASS' if g1_pass else 'FAIL'}"
                )
    else:
        lines.append("**Actual wake:** not annotated")

    fallback_str = "yes" if day.fallback_used else "no"
    lines += [f"**Fallback used:** {fallback_str}", ""]

    # ── Tick counts ───────────────────────────────────────────────────────
    lines += [
        "## Tick counts",
        "",
        f"- Ticks sent: {day.ticks_sent}",
        f"- Ticks received: {day.ticks_received}",
        f"- Evaluations: {total_evals}",
        f"- Deduped: {deduped}",
        f"- Outside window: {outside_window}",
        "",
    ]

    # ── Score timeline table ──────────────────────────────────────────────
    lines += ["## Score timeline", ""]
    lines.append(f"| Device TS ({abbr}) | Source | Score | Decision | Reason |")
    lines.append("|---|---|---|---|---|")

    scores_for_sparkline: list[int] = []

    for we, te in eval_rows:
        local_ts = _local_time(te.device_ts, tz)
        source = te.wake_source
        score = we.score
        decision = we.decision
        # Build reason string from breakdown_json.
        reason = ""
        try:
            import json as _json

            bd = _json.loads(we.breakdown_json)
            cr = bd.get("confirm_reason")
            if cr:
                reason = cr
            elif bd.get("notes"):
                reason = bd["notes"][0] if bd["notes"] else ""
        except Exception as exc:
            log.debug("report_reason_parse_error", exc_info=exc)
        lines.append(f"| {local_ts} | {source} | {score} | {decision} | {reason} |")

        if we.gate == EvaluationGate.open.value:
            scores_for_sparkline.append(score)

    lines.append("")

    # Sparkline.
    if scores_for_sparkline:
        spark = sparkline(scores_for_sparkline)
        lines += ["**Sparkline** (score over time, fixed 0-100 scale):", "```", spark, "```", ""]

    # ── State transitions ─────────────────────────────────────────────────
    lines += ["## State transitions", ""]
    if transitions:
        for tr in transitions:
            ts_local = _local_time(tr.at, tz)
            lines.append(
                f"- **{ts_local} {abbr}:** {tr.from_state} -> {tr.to_state} (cause: {tr.cause})"
            )
    else:
        lines.append("- No transitions recorded")
    lines.append("")

    # ── Push notifications ────────────────────────────────────────────────
    lines += ["## Push notifications", ""]
    total_tick_pushes = len(tick_pushes)
    ok_tick_pushes = sum(1 for p in tick_pushes if p.ticket_status == "ok")
    lines += [
        f"- Silent ticks: {total_tick_pushes} sent, {ok_tick_pushes} with ok ticket",
    ]
    if push_errors:
        for err, count in push_errors.items():
            lines.append(f"- Error: {err} = {count}")
    else:
        lines.append("- Errors: none")
    lines.append("")

    # ── Scoring metadata ─────────────────────────────────────────────────
    lines += ["## Scoring metadata", ""]
    lines += [
        f"- **Rule version:** {rule_version_num}"
        + (f" (sha256: {rule_version_sha}...)" if rule_version_sha else ""),
        f"- **Day created:** {day.created_at}",
        f"- **Report generated:** {now.strftime('%Y-%m-%dT%H:%M:%SZ')}",
        "",
    ]

    # ── Notes / anomalies ────────────────────────────────────────────────
    lines += ["## Notes", ""]
    anomalies: list[str] = []

    # 1. Force-quit: zero silent_push events inside window.
    n_silent_in_window = sum(
        1
        for we, te in eval_rows
        if te.wake_source == "silent_push" and we.gate == EvaluationGate.open.value
    )
    if n_silent_in_window == 0 and day.state not in {"idle", "monitoring"}:
        anomalies.append("Force-quit suspected: no silent push events recorded in window")

    # 2. Pedometer denied.
    pedometer_denied = any(te.pedometer_auth == "denied" for _, te in eval_rows)
    if pedometer_denied:
        anomalies.append("Pedometer denied: reweighting applied")

    # 3. > 20% deduped.
    if total_evals > 0 and deduped / total_evals > 0.20:
        pct = int(deduped / total_evals * 100)
        anomalies.append(f"> 20% ticks deduped ({pct}%)")

    # 4. ticks_sent - ticks_received >= 3.
    lost = day.ticks_sent - day.ticks_received
    if lost >= 3:
        anomalies.append(f"{lost} ticks lost (sent={day.ticks_sent} received={day.ticks_received})")

    # 5. Push errors.
    if push_errors:
        err_parts = ", ".join(f"{k}={v}" for k, v in push_errors.items())
        anomalies.append(f"Push errors: {err_parts}")

    # 6. Fallback used.
    if day.fallback_used:
        anomalies.append("Fallback used: confirm window expired without confirmation")

    if anomalies:
        for a in anomalies:
            lines.append(f"- Warning: {a}")
    else:
        lines.append("- No anomalies detected")
    lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# File writer
# ---------------------------------------------------------------------------


def write_day_report(
    content: str,
    local_date: datetime.date,
    reports_dir: Path,
) -> Path:
    """Write *content* to ``reports_dir/{local_date}.md`` (idempotent overwrite).

    Creates the directory if it does not exist.

    Args:
        content:     Markdown string to write.
        local_date:  The local calendar date (used as file name).
        reports_dir: Directory to write into.

    Returns:
        The :class:`Path` of the written file.
    """
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_file = reports_dir / f"{local_date.isoformat()}.md"
    report_file.write_text(content, encoding="utf-8")
    log.info("report_written", path=str(report_file))
    return report_file
