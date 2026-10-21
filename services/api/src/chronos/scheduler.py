"""Chronos-AI scheduler: pure planner + asyncio Scheduler class (T1.7).

Architecture
------------
``plan()`` is a **pure function** — no I/O — that accepts the current state
and returns a list of :class:`Action` objects.  This enables exact unit tests
and minute-by-minute simulation without touching the DB.

``Scheduler`` executes the plan: it queries the DB, calls ``plan()``, and
performs the resulting actions (DB writes + push calls).  It runs as an
asyncio background task started in the FastAPI lifespan.

Public API
----------
- :class:`StartMonitoring` — transition idle → monitoring.
- :class:`SendTick`        — send one silent push tick.
- :class:`Fallback`        — transition monitoring → fallback at window end.
- :class:`Noop`            — no action; carries a reason string.
- :data:`Action`           — union type of all actions.
- :func:`plan`             — pure planner; no I/O.
- :class:`Scheduler`       — stateful executor with injectable clock + push client.
"""

from __future__ import annotations

import asyncio
import datetime
import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog
from sqlmodel import Session, select

from chronos.enums import DayState, PushKind, TicketStatus, TransitionCause
from chronos.models.delivery import PushNotification
from chronos.models.devices import Device
from chronos.models.wake import Day, DayTransition
from chronos.time import format_utc_iso
from chronos.wake.rules import get_rules_cache
from chronos.wake.state import get_or_create_day
from chronos.wake.window import local_date_for, tick_instants, window_for

if TYPE_CHECKING:
    from chronos.config import Settings
    from chronos.delivery.push import PushClient
    from chronos.wake.window import Window

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Action types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StartMonitoring:
    """Transition idle → monitoring at window start."""


@dataclass(frozen=True)
class SendTick:
    """Send one silent push tick."""

    tick_seq: int
    instant: datetime.datetime  # UTC-aware; the tick_instants entry this corresponds to


@dataclass(frozen=True)
class Fallback:
    """Transition monitoring → fallback at window end."""


@dataclass(frozen=True)
class Noop:
    """No action required; *reason* is for logging."""

    reason: str


Action = StartMonitoring | SendTick | Fallback | Noop

# ---------------------------------------------------------------------------
# Pure planner
# ---------------------------------------------------------------------------

#: Terminal states — no further action from the scheduler.
_TERMINAL_STATES = {DayState.confirmed.value, DayState.fallback.value, DayState.delivered.value}


def plan(
    now: datetime.datetime,
    device: Device,
    day: Day,
    window: Window,
    sent_tick_seqs: frozenset[int],
    last_rate_limited_at: datetime.datetime | None,
    tick_interval_min: int = 5,
) -> list[Action]:
    """Pure planner — no I/O.

    Rules (evaluated in order; first matching rule wins):

    1. ``day.state`` is confirmed / fallback / delivered
       → ``[Noop("already_terminal")]``
    2. ``now >= window.end_utc`` and ``day.state == monitoring``
       → ``[Fallback()]`` (window closed without confirmation)
    3. ``now < window.start_utc``
       → ``[Noop("before_window")]``
    4. ``now >= window.start_utc`` and ``day.state == idle``
       → ``[StartMonitoring()]``
    5. ``day.state == monitoring`` → find the next due tick:
       - Compute all tick instants for the window.
       - A tick is *due* if its instant ≤ now and its seq is not in sent_tick_seqs.
       - If rate-limited: skip the instant == last_rate_limited_at (treat as already sent).
       - Return ``[SendTick(seq, instant)]`` for the earliest due tick not yet sent.
    6. Any other state (idle, window not yet started) → ``[Noop("idle_in_window")]``

    Args:
        now:                  UTC-aware current time.
        device:               The device row (used for identity; no I/O here).
        day:                  The Day row for (device, local_date).
        window:               Pre-computed Window for this day.
        sent_tick_seqs:       Frozenset of tick_seq values already recorded in push_notifications.
        last_rate_limited_at: UTC instant of the most recent MessageRateExceeded error, or None.

    Returns:
        A list containing exactly one :data:`Action`.
    """
    if now.tzinfo is None:
        raise ValueError("plan(): now must be UTC-aware")

    state = day.state

    # Rule 1: terminal states
    if state in _TERMINAL_STATES:
        return [Noop("already_terminal")]

    # Rule 2: window end + monitoring → fallback
    if now >= window.end_utc and state == DayState.monitoring.value:
        return [Fallback()]

    # Rule 3: before window
    if now < window.start_utc:
        return [Noop("before_window")]

    # now is in [window.start_utc, window.end_utc)
    # Rule 4: window start + idle → start monitoring
    if state == DayState.idle.value:
        return [StartMonitoring()]

    # Rule 5: monitoring → find next due tick
    if state == DayState.monitoring.value:
        instants = tick_instants(window, tick_interval_min)

        for seq, instant in enumerate(instants):
            if instant > now:
                break  # not yet due
            if seq in sent_tick_seqs:
                continue  # already sent
            # Skip the rate-limited instant
            if last_rate_limited_at is not None and instant == last_rate_limited_at:
                continue
            return [SendTick(tick_seq=seq, instant=instant)]

        return [Noop("all_due_ticks_sent")]

    # Rule 6: any other case (idle after window end, etc.)
    return [Noop("idle_in_window")]


# ---------------------------------------------------------------------------
# on_fallback hook (no-op; T5.2 wires delivery)
# ---------------------------------------------------------------------------


def on_fallback(session: Session, day: Day) -> None:
    """No-op hook called after monitoring → fallback.

    T5.2 will replace this with the delivery trigger.
    """
    log.debug("on_fallback_noop", local_date=day.local_date, device_id=day.device_id)


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------


class Scheduler:
    """Asyncio-based scheduler for Chronos-AI.

    Drives the morning routine:
    - Every 60 s: ``run_due(now)`` checks all active devices and applies
      the planner's actions (transitions + tick sends).
    - Every 15 min: ``run_receipts(now)`` fetches Expo receipts for sent ticks.

    The clock is injectable for deterministic testing.

    Usage::

        scheduler = Scheduler(
            session_factory=lambda: Session(engine),
            push_client=ExpoPushClient(...),
            settings=settings,
        )
        stop_event = asyncio.Event()
        task = asyncio.create_task(scheduler.run_forever(stop_event))
        # ... later:
        stop_event.set()
        await task
    """

    RECEIPT_DELAY_MIN: int = 15  # minimum age before checking receipts

    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        push_client: PushClient,
        settings: Settings,
        clock: Callable[[], datetime.datetime] | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._push_client = push_client
        self._settings = settings
        self._clock: Callable[[], datetime.datetime] = clock or _utc_now

        # device_id → next scheduled tick instant (UTC); None if not monitoring.
        self._next_tick_instants: dict[int, datetime.datetime | None] = {}
        # device_id → local_date (str) for "warned no token" deduplication.
        self._warned_no_token: dict[int, str] = {}

    def get_next_tick_for(self, device_id: int) -> datetime.datetime | None:
        """Return the next scheduled tick UTC instant for *device_id*, or None."""
        return self._next_tick_instants.get(device_id)

    # ------------------------------------------------------------------
    # run_due
    # ------------------------------------------------------------------

    async def run_due(self, now: datetime.datetime) -> None:
        """Check all active devices and apply the planner's actions.

        Called every 60 s by :meth:`run_forever`.
        """
        with self._session_factory() as session:
            devices: list[Device] = list(
                session.exec(  # type: ignore[call-overload]
                    select(Device).where(Device.status == "active")
                ).all()
            )
            for device in devices:
                try:
                    await self._process_device(session, device, now)
                except Exception:
                    log.exception(
                        "scheduler_device_error",
                        device_id=device.id,
                    )
            session.commit()

    async def _process_device(
        self,
        session: Session,
        device: Device,
        now: datetime.datetime,
    ) -> None:
        """Process one device: compute window, run planner, execute actions."""
        assert device.id is not None

        # Token check: warn once per day
        if device.expo_push_token is None:
            from zoneinfo import ZoneInfo

            rules, _ = get_rules_cache().get_effective(session)
            tz_obj = ZoneInfo(device.tz)
            local_date = local_date_for(now, tz_obj, rules.window.to_window_config().day_boundary)
            local_date_str = local_date.isoformat()
            if self._warned_no_token.get(device.id) != local_date_str:
                log.warning(
                    "scheduler_no_push_token",
                    device_id=device.id,
                    local_date=local_date_str,
                )
                self._warned_no_token[device.id] = local_date_str
            self._next_tick_instants[device.id] = None
            return

        from zoneinfo import ZoneInfo

        rules, _rule_version = get_rules_cache().get_effective(session)
        window_cfg = rules.window.to_window_config()
        tz_obj = ZoneInfo(device.tz)
        local_date = local_date_for(now, tz_obj, window_cfg.day_boundary)
        local_date_str = local_date.isoformat()

        window = window_for(local_date, tz_obj, window_cfg)
        day = get_or_create_day(session, device, local_date, device.tz, rules, now)
        session.flush()

        # Derive sent tick_seqs from push_notifications
        sent_tick_seqs = self._get_sent_tick_seqs(session, device.id, local_date_str)

        # Derive last rate-limited instant
        last_rate_limited_at = self._get_last_rate_limited_at(
            session, device.id, local_date_str, window, window_cfg.tick_interval_min
        )

        actions = plan(
            now,
            device,
            day,
            window,
            sent_tick_seqs,
            last_rate_limited_at,
            tick_interval_min=window_cfg.tick_interval_min,
        )

        for action in actions:
            if isinstance(action, StartMonitoring):
                await self._do_start_monitoring(session, device, day, now)
            elif isinstance(action, SendTick):
                await self._do_send_tick(
                    session, device, day, action.tick_seq, action.instant, window, now
                )
            elif isinstance(action, Fallback):
                await self._do_fallback(session, device, day, now)
            else:
                log.debug(
                    "scheduler_noop",
                    device_id=device.id,
                    local_date=local_date_str,
                    reason=action.reason,
                )

        # Update next_tick_instants cache
        if day.state == DayState.monitoring.value:
            self._update_next_tick(
                device.id, window, window_cfg.tick_interval_min, session, local_date_str, now
            )
        else:
            self._next_tick_instants[device.id] = None

    def _get_sent_tick_seqs(
        self,
        session: Session,
        device_id: int,
        local_date: str,
    ) -> frozenset[int]:
        """Query push_notifications for already-sent tick_seqs."""
        rows = session.exec(  # type: ignore[call-overload]
            select(PushNotification.tick_seq).where(
                PushNotification.device_id == device_id,
                PushNotification.local_date == local_date,
                PushNotification.kind == PushKind.tick.value,
                PushNotification.tick_seq.is_not(None),  # type: ignore[union-attr]
            )
        ).all()
        return frozenset(seq for seq in rows if seq is not None)

    def _get_last_rate_limited_at(
        self,
        session: Session,
        device_id: int,
        local_date: str,
        window: Window,
        interval_min: int,
    ) -> datetime.datetime | None:
        """Return the tick instant of the most recent MessageRateExceeded error."""
        row = session.exec(  # type: ignore[call-overload]
            select(PushNotification.tick_seq)
            .where(
                PushNotification.device_id == device_id,
                PushNotification.local_date == local_date,
                PushNotification.kind == PushKind.tick.value,
                PushNotification.error == "MessageRateExceeded",
            )
            .order_by(PushNotification.sent_at.desc())  # type: ignore[union-attr]
        ).first()
        if row is None:
            return None
        # Convert the tick_seq back to its UTC instant
        instants = tick_instants(window, interval_min)
        if row < len(instants):
            return instants[row]
        return None

    def _update_next_tick(
        self,
        device_id: int,
        window: Window,
        interval_min: int,
        session: Session,
        local_date: str,
        now: datetime.datetime,
    ) -> None:
        """Update the next tick instant cache for the telemetry response."""
        sent = self._get_sent_tick_seqs(session, device_id, local_date)
        instants = tick_instants(window, interval_min)
        for seq, instant in enumerate(instants):
            if seq not in sent and instant > now:
                self._next_tick_instants[device_id] = instant
                return
        self._next_tick_instants[device_id] = None

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    async def _do_start_monitoring(
        self,
        session: Session,
        device: Device,
        day: Day,
        now: datetime.datetime,
    ) -> None:
        """Insert idle→monitoring transition row."""
        now_str = format_utc_iso(now)
        dt = DayTransition(
            local_date=day.local_date,
            device_id=day.device_id,
            from_state=DayState.idle.value,
            to_state=DayState.monitoring.value,
            cause=TransitionCause.scheduler_window_start.value,
            caused_by_evaluation_id=None,
            at=now_str,
        )
        session.add(dt)
        day.state = DayState.monitoring.value
        day.updated_at = now_str
        session.add(day)
        session.flush()
        log.info(
            "day_transition",
            local_date=day.local_date,
            device_id=device.id,
            from_state="idle",
            to_state="monitoring",
            cause="scheduler_window_start",
        )

    async def _do_send_tick(
        self,
        session: Session,
        device: Device,
        day: Day,
        tick_seq: int,
        instant: datetime.datetime,
        window: Window,
        now: datetime.datetime,
    ) -> None:
        """Send one silent push tick and record the result in push_notifications."""
        assert device.expo_push_token is not None
        assert device.id is not None

        from zoneinfo import ZoneInfo

        tz_obj = ZoneInfo(device.tz)
        window_start_local = window.start_utc.astimezone(tz_obj).isoformat()

        from chronos.delivery.push import PushMessage

        msg = PushMessage(
            to=device.expo_push_token,
            data={
                "type": "tick",
                "local_date": day.local_date,
                "tick_seq": tick_seq,
                "window_start": window_start_local,
            },
        )

        # Send (push client handles retries internally)
        tickets = await self._push_client.send([msg])
        ticket = tickets[0] if tickets else None

        now_str = format_utc_iso(now)
        ticket_id: str | None = None
        ticket_status = TicketStatus.error.value
        error: str | None = None

        if ticket is not None:
            ticket_status = ticket.status
            if ticket.status == TicketStatus.ok.value:
                ticket_id = ticket.id
            else:
                error = ticket.message

        # Build payload_json without token
        payload_dict = msg.to_payload_dict_without_token()

        pn = PushNotification(
            device_id=device.id,
            kind=PushKind.tick.value,
            local_date=day.local_date,
            reel_id=None,
            tick_seq=tick_seq,
            payload_json=json.dumps(payload_dict),
            ticket_id=ticket_id,
            ticket_status=ticket_status,
            receipt_status=None,
            sent_at=now_str,
            receipt_checked_at=None,
            error=error,
        )
        session.add(pn)

        if ticket_status == TicketStatus.ok.value:
            day.ticks_sent = (day.ticks_sent or 0) + 1
            day.updated_at = now_str
            session.add(day)
            log.info(
                "tick_sent",
                device_id=device.id,
                local_date=day.local_date,
                tick_seq=tick_seq,
                ticket_id=ticket_id,
            )
        else:
            log.warning(
                "tick_error",
                device_id=device.id,
                local_date=day.local_date,
                tick_seq=tick_seq,
                error=error,
            )
            if error == "DeviceNotRegistered":
                # Clear the token — device re-registered later via PATCH /v1/device/me
                device.expo_push_token = None
                session.add(device)
                log.warning(
                    "device_token_cleared",
                    device_id=device.id,
                    local_date=day.local_date,
                    reason="DeviceNotRegistered",
                )
            # MessageRateExceeded: the error is stored in push_notifications; on the
            # next run_due the rate-limited seq will be in sent_tick_seqs, so plan()
            # skips it and advances to the next tick instant (one interval later).

        session.flush()

    async def _do_fallback(
        self,
        session: Session,
        device: Device,
        day: Day,
        now: datetime.datetime,
    ) -> None:
        """Transition monitoring → fallback at window end."""
        if day.state != DayState.monitoring.value:
            log.debug(
                "scheduler_skip_fallback",
                device_id=device.id,
                local_date=day.local_date,
                state=day.state,
            )
            return

        now_str = format_utc_iso(now)
        dt = DayTransition(
            local_date=day.local_date,
            device_id=day.device_id,
            from_state=DayState.monitoring.value,
            to_state=DayState.fallback.value,
            cause=TransitionCause.scheduler_window_end.value,
            caused_by_evaluation_id=None,
            at=now_str,
        )
        session.add(dt)
        day.state = DayState.fallback.value
        day.fallback_used = 1
        day.updated_at = now_str
        session.add(day)
        session.flush()

        log.info(
            "day_transition",
            local_date=day.local_date,
            device_id=device.id,
            from_state="monitoring",
            to_state="fallback",
            cause="scheduler_window_end",
        )
        on_fallback(session, day)

    # ------------------------------------------------------------------
    # run_receipts
    # ------------------------------------------------------------------

    async def run_receipts(self, now: datetime.datetime) -> None:
        """Fetch Expo receipts for tickets ≥ 15 min old without receipt_status."""
        cutoff = now - datetime.timedelta(minutes=self.RECEIPT_DELAY_MIN)
        cutoff_str = format_utc_iso(cutoff)

        with self._session_factory() as session:
            rows: list[PushNotification] = list(
                session.exec(  # type: ignore[call-overload]
                    select(PushNotification).where(
                        PushNotification.receipt_status.is_(None),  # type: ignore[union-attr]
                        PushNotification.ticket_id.is_not(None),  # type: ignore[union-attr]
                        PushNotification.ticket_status == TicketStatus.ok.value,
                        PushNotification.sent_at < cutoff_str,
                    )
                ).all()
            )

            if not rows:
                return

            ticket_ids = [r.ticket_id for r in rows if r.ticket_id is not None]
            receipts = await self._push_client.get_receipts(ticket_ids)

            now_str = format_utc_iso(now)
            for row in rows:
                if row.ticket_id is None:
                    continue
                receipt = receipts.get(row.ticket_id)
                if receipt is not None:
                    row.receipt_status = receipt.status
                    row.receipt_checked_at = now_str
                    session.add(row)
                    log.info(
                        "receipt_recorded",
                        ticket_id=row.ticket_id,
                        receipt_status=receipt.status,
                        device_id=row.device_id,
                    )

            session.commit()
            log.info("receipts_checked", count=len(rows), found=len(receipts))

    # ------------------------------------------------------------------
    # run_forever
    # ------------------------------------------------------------------

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        """Main loop: run_due every 60 s, run_receipts every 15 min.

        Stops when *stop_event* is set.  Exceptions inside run_due /
        run_receipts are caught and logged (the loop continues).
        """
        last_receipts_check = self._clock() - datetime.timedelta(minutes=self.RECEIPT_DELAY_MIN)

        while not stop_event.is_set():
            now = self._clock()

            try:
                await self.run_due(now)
            except Exception:
                log.exception("scheduler_run_due_error")

            if (now - last_receipts_check) >= datetime.timedelta(minutes=self.RECEIPT_DELAY_MIN):
                try:
                    await self.run_receipts(now)
                except Exception:
                    log.exception("scheduler_run_receipts_error")
                last_receipts_check = now

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=60.0)
            except TimeoutError:
                pass


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(tz=datetime.UTC)
