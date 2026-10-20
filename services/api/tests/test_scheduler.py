"""Tests for chronos.scheduler — plan() pure function + Scheduler simulation.

Part 1: plan() unit tests (no DB, no I/O).
Part 2: Scheduler simulation with FakeClock + FakePushClient + temp DB.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from sqlmodel import select

from chronos.delivery.push import FakePushClient
from chronos.enums import DayState, PushKind, TransitionCause
from chronos.models.delivery import PushNotification
from chronos.models.wake import Day, DayTransition
from chronos.scheduler import (
    Action,
    Fallback,
    Noop,
    Scheduler,
    SendTick,
    StartMonitoring,
    plan,
)
from chronos.wake.window import Window, tick_instants, window_for

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------

UTC = datetime.UTC
TZ = ZoneInfo("America/Los_Angeles")  # UTC-7 (PDT during summer)

# Weekday window: 06:00-11:00 local = 13:00-18:00 UTC (PDT offset -7)
WEEKDAY_DATE = datetime.date(2026, 10, 6)  # Tuesday


def _utc(hour: int, minute: int = 0, date: datetime.date = WEEKDAY_DATE) -> datetime.datetime:
    """Build a UTC-aware datetime for the test date."""
    # For America/Los_Angeles on 2026-10-06: PDT (UTC-7)
    # 06:00 local = 13:00 UTC
    return datetime.datetime(date.year, date.month, date.day, hour, minute, tzinfo=UTC)


def _make_window() -> Window:
    """Weekday window for 2026-10-06 in America/Los_Angeles."""
    from chronos.wake.window import WindowConfig

    cfg = WindowConfig(
        weekday_start=datetime.time(6, 0),
        weekday_end=datetime.time(11, 0),
        weekend_start=datetime.time(8, 0),
        weekend_end=datetime.time(14, 0),
    )
    return window_for(WEEKDAY_DATE, TZ, cfg)


def _make_weekend_window() -> Window:
    """Weekend window for 2026-10-10 (Saturday) in America/Los_Angeles."""
    from chronos.wake.window import WindowConfig

    cfg = WindowConfig(
        weekday_start=datetime.time(6, 0),
        weekday_end=datetime.time(11, 0),
        weekend_start=datetime.time(8, 0),
        weekend_end=datetime.time(14, 0),
    )
    return window_for(datetime.date(2026, 10, 10), TZ, cfg)


def _fake_device(expo_token: str | None = "ExponentPushToken[abc]") -> object:  # noqa: S107
    """Return a Device for plan() tests (not persisted)."""
    import hashlib

    from chronos.models.devices import Device

    return Device.model_validate(
        {
            "id": 1,
            "public_id": "fake-public-id",
            "name": "Test iPhone",
            "token_hash": hashlib.sha256(b"tok").hexdigest(),
            "expo_push_token": expo_token,
            "tz": "America/Los_Angeles",
            "app_version": "1.0.0",
            "ios_version": "17.5",
            "model": "iPhone 15 Pro",
            "status": "active",
            "created_at": "2026-10-06T00:00:00.000000Z",
            "last_seen_at": "2026-10-06T00:00:00.000000Z",
            "revoked_at": None,
        }
    )


def _fake_day(
    *,
    state: str = DayState.idle.value,
    ticks_sent: int = 0,
) -> object:
    """Return a Day for plan() tests (not persisted)."""
    from chronos.models.wake import Day

    return Day.model_validate(
        {
            "local_date": WEEKDAY_DATE.isoformat(),
            "device_id": 1,
            "tz": "America/Los_Angeles",
            "kind": "weekday",
            "window_start_utc": "2026-10-06T13:00:00.000000Z",
            "window_end_utc": "2026-10-06T18:00:00.000000Z",
            "state": state,
            "confirmed_at": None,
            "confirm_evaluation_id": None,
            "delivered_at": None,
            "fallback_used": 0,
            "actual_wake_local": None,
            "ticks_sent": ticks_sent,
            "ticks_received": 0,
            "brief_id": None,
            "reel_id": None,
            "created_at": "2026-10-06T00:00:00.000000Z",
            "updated_at": "2026-10-06T00:00:00.000000Z",
        }
    )


# ---------------------------------------------------------------------------
# Part 1: plan() unit tests
# ---------------------------------------------------------------------------


class TestPlan:
    def _plan(
        self,
        now: datetime.datetime,
        *,
        state: str = DayState.idle.value,
        sent: frozenset[int] = frozenset(),
        last_rate_limited_at: datetime.datetime | None = None,
        window: Window | None = None,
        interval_min: int = 5,
    ) -> list[Action]:
        w = window if window is not None else _make_window()
        device = _fake_device()
        day = _fake_day(state=state)
        return plan(now, device, day, w, sent, last_rate_limited_at, interval_min)

    def test_before_window_noop(self) -> None:
        window = _make_window()
        # 1 second before window start
        now = window.start_utc - datetime.timedelta(seconds=1)
        result = self._plan(now, window=window)
        assert len(result) == 1
        assert isinstance(result[0], Noop)
        assert result[0].reason == "before_window"

    def test_start_monitoring_at_window_start(self) -> None:
        window = _make_window()
        now = window.start_utc
        result = self._plan(now, state=DayState.idle.value, window=window)
        assert len(result) == 1
        assert isinstance(result[0], StartMonitoring)

    def test_start_monitoring_mid_window(self) -> None:
        """Idle device partway through window → StartMonitoring."""
        window = _make_window()
        now = window.start_utc + datetime.timedelta(minutes=30)
        result = self._plan(now, state=DayState.idle.value, window=window)
        assert isinstance(result[0], StartMonitoring)

    def test_send_first_tick(self) -> None:
        window = _make_window()
        now = window.start_utc
        result = self._plan(now, state=DayState.monitoring.value, sent=frozenset(), window=window)
        assert len(result) == 1
        assert isinstance(result[0], SendTick)
        assert result[0].tick_seq == 0
        assert result[0].instant == window.start_utc

    def test_send_nth_tick(self) -> None:
        """After 5 ticks sent (0 to 4), next due tick is seq 5."""
        window = _make_window()
        now = window.start_utc + datetime.timedelta(minutes=25)
        result = self._plan(
            now,
            state=DayState.monitoring.value,
            sent=frozenset({0, 1, 2, 3, 4}),
            window=window,
        )
        assert isinstance(result[0], SendTick)
        assert result[0].tick_seq == 5

    def test_all_due_ticks_sent_noop(self) -> None:
        """If all due ticks already sent, return Noop."""
        window = _make_window()
        now = window.start_utc + datetime.timedelta(minutes=5)
        # ticks at +0 and +5 are both due; both sent
        result = self._plan(
            now,
            state=DayState.monitoring.value,
            sent=frozenset({0, 1}),
            window=window,
        )
        assert isinstance(result[0], Noop)

    def test_confirmed_noop(self) -> None:
        window = _make_window()
        now = window.start_utc + datetime.timedelta(minutes=10)
        result = self._plan(now, state=DayState.confirmed.value, window=window)
        assert isinstance(result[0], Noop)
        assert result[0].reason == "already_terminal"

    def test_fallback_noop(self) -> None:
        window = _make_window()
        now = window.end_utc + datetime.timedelta(minutes=5)
        result = self._plan(now, state=DayState.fallback.value, window=window)
        assert isinstance(result[0], Noop)

    def test_fallback_at_window_end(self) -> None:
        """Monitoring day at exact window end → Fallback."""
        window = _make_window()
        now = window.end_utc
        result = self._plan(now, state=DayState.monitoring.value, window=window)
        assert isinstance(result[0], Fallback)

    def test_no_tick_at_window_end(self) -> None:
        """window.end_utc is exclusive; no tick sent at end."""
        window = _make_window()
        now = window.end_utc
        result = self._plan(now, state=DayState.monitoring.value, sent=frozenset(), window=window)
        # Should be Fallback, not SendTick
        assert isinstance(result[0], Fallback)

    def test_rate_limited_skips_instant(self) -> None:
        """MessageRateExceeded on tick at +5min → next tick is at +10min."""
        window = _make_window()
        rate_limited_instant = window.start_utc + datetime.timedelta(minutes=5)
        now = window.start_utc + datetime.timedelta(minutes=10)
        # Tick 0 (at start_utc) sent; tick 1 (at start+5) rate-limited; tick 2 due at start+10
        result = self._plan(
            now,
            state=DayState.monitoring.value,
            sent=frozenset({0}),  # tick 0 sent; tick 1 was rate-limited (not in sent_tick_seqs)
            last_rate_limited_at=rate_limited_instant,
            window=window,
        )
        assert isinstance(result[0], SendTick)
        assert result[0].tick_seq == 2  # seq 1 skipped, seq 2 is next

    def test_weekend_window(self) -> None:
        """Weekend window 08:00-14:00 local; ticks at 5-min intervals."""
        window = _make_weekend_window()
        now = window.start_utc
        result = self._plan(now, state=DayState.monitoring.value, sent=frozenset(), window=window)
        assert isinstance(result[0], SendTick)
        assert result[0].tick_seq == 0
        assert result[0].instant == window.start_utc

    def test_tick_at_last_instant_before_end(self) -> None:
        """Last valid tick instant (< end_utc) should be sent."""
        window = _make_window()
        instants = tick_instants(window, 5)
        last_instant = instants[-1]
        assert last_instant < window.end_utc
        now = last_instant
        # All previous ticks sent
        sent = frozenset(range(len(instants) - 1))
        result = self._plan(now, state=DayState.monitoring.value, sent=sent, window=window)
        assert isinstance(result[0], SendTick)
        assert result[0].tick_seq == len(instants) - 1

    def test_naive_now_raises(self) -> None:
        window = _make_window()
        naive_now = datetime.datetime(2026, 10, 6, 13, 0)  # no tzinfo
        device = _fake_device()
        day = _fake_day()
        with pytest.raises(ValueError, match="UTC-aware"):
            plan(naive_now, device, day, window, frozenset(), None)

    def test_60_ticks_in_weekday_window(self) -> None:
        """Weekday 06:00-11:00 at 5-min intervals = 60 ticks (0..59)."""
        window = _make_window()
        instants = tick_instants(window, 5)
        assert len(instants) == 60
        assert instants[0] == window.start_utc
        assert instants[-1] < window.end_utc


# ---------------------------------------------------------------------------
# Part 2: Scheduler simulation
# ---------------------------------------------------------------------------


def _make_test_settings(tmp_path: Path) -> object:
    """Build minimal Settings for scheduler tests."""
    from tests.conftest import make_settings

    db_url = f"sqlite:///{tmp_path / 'sched.db'}"
    from alembic import command
    from alembic.config import Config

    ini_path = Path(__file__).parents[1] / "alembic.ini"
    cfg = Config(str(ini_path))
    cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(cfg, "head")
    return make_settings(
        database_url=db_url,
        scheduler_enabled=False,
        skip_rules_init=False,
    )


def _make_session_factory(settings: object) -> object:
    from chronos.db import create_db_engine

    engine = create_db_engine(settings.database_url)  # type: ignore[union-attr]
    from sqlmodel import Session as _Session

    return lambda: _Session(engine)


async def _run_scheduler_step(
    scheduler: Scheduler,
    now: datetime.datetime,
) -> None:
    """Run one run_due step at `now`."""
    await scheduler.run_due(now)


class TestSchedulerSimulation:
    """Full-day simulation: 05:50 → 11:05 in 1-min steps."""

    @pytest.fixture()
    def setup(self, tmp_path: Path) -> dict:  # type: ignore[type-arg]
        """Set up scheduler with warm rules cache and a device with push token."""
        settings = _make_test_settings(tmp_path)
        session_factory = _make_session_factory(settings)

        # Warm the rules cache
        from chronos.wake.rules import ensure_initial_version, get_rules_cache, reset_rules_cache

        reset_rules_cache()
        with session_factory() as session:
            rules_path = Path(__file__).parents[1] / "config" / "wake_rules.yaml"
            ensure_initial_version(session, rules_path)
            session.commit()
            # Warm cache
            get_rules_cache().get_effective(session)

        fake_push = FakePushClient()
        scheduler = Scheduler(
            session_factory=session_factory,
            push_client=fake_push,
            settings=settings,  # type: ignore[arg-type]
        )

        # Create device with token
        with session_factory() as session:
            from tests.factories import make_device

            device = make_device(
                session,
                tz="America/Los_Angeles",
                expo_push_token="ExponentPushToken[sim_test]",
                status="active",
            )
            session.commit()
            device_id: int = device.id  # type: ignore[assignment]

        return {
            "scheduler": scheduler,
            "fake_push": fake_push,
            "session_factory": session_factory,
            "device_id": device_id,
        }

    @pytest.mark.anyio
    async def test_full_weekday_60_ticks(self, setup: dict) -> None:  # type: ignore[type-arg]
        """Simulate 05:50-11:05 in 1-min steps.

        Expect 60 ticks + StartMonitoring + Fallback."""
        scheduler: Scheduler = setup["scheduler"]
        fake_push: FakePushClient = setup["fake_push"]
        session_factory = setup["session_factory"]
        device_id: int = setup["device_id"]

        # America/Los_Angeles on 2026-10-06 is PDT (UTC-7)
        # 06:00 local = 13:00 UTC, 11:00 local = 18:00 UTC
        window = _make_window()

        # Simulate 05:50 to 11:05 local = 12:50 to 18:05 UTC in 1-min steps
        start_sim = window.start_utc - datetime.timedelta(minutes=10)
        end_sim = window.end_utc + datetime.timedelta(minutes=5)

        current = start_sim
        while current <= end_sim:
            await scheduler.run_due(current)
            current += datetime.timedelta(minutes=1)

        # Assertions
        with session_factory() as session:
            device_id_val = device_id
            local_date = WEEKDAY_DATE.isoformat()

            # 60 ticks sent
            ticks = list(
                session.exec(  # type: ignore[call-overload]
                    select(PushNotification).where(
                        PushNotification.device_id == device_id_val,
                        PushNotification.local_date == local_date,
                        PushNotification.kind == PushKind.tick.value,
                    )
                ).all()
            )
            assert len(ticks) == 60, f"Expected 60 ticks, got {len(ticks)}"

            # Tick seqs 0-59
            seqs = sorted(t.tick_seq for t in ticks)
            assert seqs == list(range(60))

            # All tickets ok
            assert all(t.ticket_status == "ok" for t in ticks)

            # StartMonitoring transition
            transitions = list(
                session.exec(  # type: ignore[call-overload]
                    select(DayTransition).where(
                        DayTransition.device_id == device_id_val,
                        DayTransition.local_date == local_date,
                    )
                ).all()
            )
            start_trans = [
                t for t in transitions if t.cause == TransitionCause.scheduler_window_start.value
            ]
            assert len(start_trans) == 1, f"Expected 1 StartMonitoring; got {start_trans}"

            # Fallback transition
            fallback_trans = [
                t for t in transitions if t.cause == TransitionCause.scheduler_window_end.value
            ]
            assert len(fallback_trans) == 1, f"Expected 1 Fallback; got {fallback_trans}"

            # Day state = fallback
            day = session.exec(  # type: ignore[call-overload]
                select(Day).where(
                    Day.device_id == device_id_val,
                    Day.local_date == local_date,
                )
            ).first()
            assert day is not None
            assert day.state == DayState.fallback.value

        # Push client received 60 messages
        assert len(fake_push.sent) == 60

    @pytest.mark.anyio
    async def test_stops_on_confirm(self, setup: dict) -> None:  # type: ignore[type-arg]
        """Confirm injected at 07:12 local → no ticks after that."""
        scheduler: Scheduler = setup["scheduler"]
        fake_push: FakePushClient = setup["fake_push"]
        session_factory = setup["session_factory"]
        device_id: int = setup["device_id"]

        window = _make_window()
        local_date = WEEKDAY_DATE.isoformat()

        # Run until 07:12 local (14:12 UTC = window_start + 72 min = 14 ticks)
        current = window.start_utc
        confirm_time = window.start_utc + datetime.timedelta(minutes=72)

        while current <= confirm_time:
            await scheduler.run_due(current)
            current += datetime.timedelta(minutes=1)

        # Inject confirm
        with session_factory() as session:
            day = session.exec(  # type: ignore[call-overload]
                select(Day).where(
                    Day.device_id == device_id,
                    Day.local_date == local_date,
                )
            ).first()
            assert day is not None
            day.state = DayState.confirmed.value
            session.add(day)
            session.commit()

        ticks_at_confirm = len(fake_push.sent)

        # Continue simulation to window end
        end_sim = window.end_utc + datetime.timedelta(minutes=5)
        while current <= end_sim:
            await scheduler.run_due(current)
            current += datetime.timedelta(minutes=1)

        # No new ticks after confirm
        assert len(fake_push.sent) == ticks_at_confirm, (
            f"Ticks sent after confirm: {len(fake_push.sent) - ticks_at_confirm}"
        )

    @pytest.mark.anyio
    async def test_rate_limited_delays_next_tick(self, setup: dict) -> None:  # type: ignore[type-arg]
        """MessageRateExceeded on tick 2 → next send skips seq 2, sends seq 3 next."""
        session_factory = setup["session_factory"]
        device_id: int = setup["device_id"]

        # Use a fake push client that errors on 3rd call
        fake_push = FakePushClient(outcomes=["ok", "ok", "MessageRateExceeded", "ok"])
        scheduler = Scheduler(
            session_factory=session_factory,
            push_client=fake_push,
            settings=setup["scheduler"]._settings,
        )

        window = _make_window()
        local_date = WEEKDAY_DATE.isoformat()

        # Timeline (each run_due step is +5 min):
        # Step 1 at start_utc: StartMonitoring (no tick)
        # Step 2 at +5: SendTick(0, start) -> ok
        # Step 3 at +10: SendTick(1, start+5) -> ok
        # Step 4 at +15: SendTick(2, start+10) -> MessageRateExceeded
        current = window.start_utc
        for _ in range(4):
            await scheduler.run_due(current)
            current += datetime.timedelta(minutes=5)

        # At this point: seqs 0, 1 ok; seq 2 rate-limited (error in push_notifications)
        with session_factory() as session:
            pns = list(
                session.exec(  # type: ignore[call-overload]
                    select(PushNotification)
                    .where(
                        PushNotification.device_id == device_id,
                        PushNotification.local_date == local_date,
                    )
                    .order_by(PushNotification.tick_seq)  # type: ignore[union-attr]
                ).all()
            )
        seqs_before = [p.tick_seq for p in pns]
        assert 2 in seqs_before, f"Expected seq 2 (rate-limited); got {seqs_before}"
        rl_pn = next(p for p in pns if p.tick_seq == 2)
        assert rl_pn.error == "MessageRateExceeded"
        assert rl_pn.ticket_status == "error"

        # Step 5 at +20: seq 2 skipped (rate-limited instant), seq 3 sent
        await scheduler.run_due(current)
        with session_factory() as session:
            pns2 = list(
                session.exec(  # type: ignore[call-overload]
                    select(PushNotification)
                    .where(
                        PushNotification.device_id == device_id,
                        PushNotification.local_date == local_date,
                    )
                    .order_by(PushNotification.tick_seq)  # type: ignore[union-attr]
                ).all()
            )
        seqs = [p.tick_seq for p in pns2]
        assert 3 in seqs, f"Expected seq 3 to be sent after rate limit; got seqs={seqs}"

    @pytest.mark.anyio
    async def test_device_not_registered_clears_token(self, setup: dict) -> None:  # type: ignore[type-arg]
        """DeviceNotRegistered → expo_push_token cleared, no further ticks."""
        session_factory = setup["session_factory"]
        device_id: int = setup["device_id"]

        fake_push = FakePushClient(outcomes=["DeviceNotRegistered"])
        scheduler = Scheduler(
            session_factory=session_factory,
            push_client=fake_push,
            settings=setup["scheduler"]._settings,
        )

        window = _make_window()

        # Step 1 at start_utc: StartMonitoring (no tick)
        # Step 2 at +1 min: SendTick(0) -> DeviceNotRegistered
        await scheduler.run_due(window.start_utc)
        await scheduler.run_due(window.start_utc + datetime.timedelta(minutes=1))

        # Token should be cleared
        with session_factory() as session:
            from chronos.models.devices import Device as DevModel

            device = session.get(DevModel, device_id)
            assert device is not None
            assert device.expo_push_token is None

        # Run again — no further ticks should be sent
        sent_before = len(fake_push.sent)
        await scheduler.run_due(window.start_utc + datetime.timedelta(minutes=5))
        assert len(fake_push.sent) == sent_before, "Ticks sent after token cleared"

    @pytest.mark.anyio
    async def test_restart_mid_window_continues_tick_seq(self, setup: dict) -> None:  # type: ignore[type-arg]
        """New Scheduler instance re-derives tick_seq from push_notifications."""
        session_factory = setup["session_factory"]
        device_id: int = setup["device_id"]

        window = _make_window()
        local_date = WEEKDAY_DATE.isoformat()

        # Pre-insert push_notifications for tick_seqs 0-5 (6 ticks)
        with session_factory() as session:
            from chronos.models.devices import Device as DevModel

            device = session.get(DevModel, device_id)
            assert device is not None
            day = session.exec(  # type: ignore[call-overload]
                select(Day).where(
                    Day.device_id == device_id,
                    Day.local_date == local_date,
                )
            ).first()
            if day is None:
                # Create the day in monitoring state
                from chronos.wake.rules import get_rules_cache

                _, _ = get_rules_cache().get_effective(session)
                day = session.exec(  # type: ignore[call-overload]
                    select(Day).where(
                        Day.device_id == device_id,
                        Day.local_date == local_date,
                    )
                ).first()
                if day is None:
                    from chronos.time import format_utc_iso

                    day = Day(
                        local_date=local_date,
                        device_id=device_id,
                        tz="America/Los_Angeles",
                        kind="weekday",
                        window_start_utc=format_utc_iso(window.start_utc),
                        window_end_utc=format_utc_iso(window.end_utc),
                        state=DayState.monitoring.value,
                        confirmed_at=None,
                        confirm_evaluation_id=None,
                        delivered_at=None,
                        fallback_used=0,
                        actual_wake_local=None,
                        ticks_sent=6,
                        ticks_received=0,
                        brief_id=None,
                        reel_id=None,
                        created_at=format_utc_iso(window.start_utc),
                        updated_at=format_utc_iso(window.start_utc),
                    )
                    session.add(day)
                    session.flush()

            # Insert 6 push_notifications rows
            from chronos.time import format_utc_iso

            for seq in range(6):
                pn = PushNotification(
                    device_id=device_id,
                    kind=PushKind.tick.value,
                    local_date=local_date,
                    tick_seq=seq,
                    payload_json='{"type":"tick"}',
                    ticket_id=f"ticket-{seq}",
                    ticket_status="ok",
                    sent_at=format_utc_iso(window.start_utc + datetime.timedelta(minutes=seq * 5)),
                )
                session.add(pn)
            session.commit()

        # Create new scheduler instance (simulates restart)
        fake_push2 = FakePushClient()
        scheduler2 = Scheduler(
            session_factory=session_factory,
            push_client=fake_push2,
            settings=setup["scheduler"]._settings,
        )

        # Run at tick_seq 6's instant (30 min into window)
        now = window.start_utc + datetime.timedelta(minutes=30)
        await scheduler2.run_due(now)

        # Check that seq 6 was sent (not 0 or any duplicate)
        with session_factory() as session:
            pns = list(
                session.exec(  # type: ignore[call-overload]
                    select(PushNotification)
                    .where(
                        PushNotification.device_id == device_id,
                        PushNotification.local_date == local_date,
                    )
                    .order_by(PushNotification.tick_seq)  # type: ignore[union-attr]
                ).all()
            )
        seqs = [p.tick_seq for p in pns]
        assert 6 in seqs, f"Expected seq 6 to be sent on restart; got seqs={seqs}"
        assert seqs.count(6) == 1, "seq 6 sent more than once"

    @pytest.mark.anyio
    async def test_receipts_recorded_after_15_min(self, setup: dict) -> None:  # type: ignore[type-arg]
        """Receipts are fetched and recorded for tickets ≥ 15 min old."""
        session_factory = setup["session_factory"]
        device_id: int = setup["device_id"]

        # Create a receipt response
        ticket_id_1 = "receipt-ticket-1"
        receipt_push = FakePushClient(
            outcomes=["ok"],
            receipt_outcomes={ticket_id_1: "ok"},
        )
        # Override the send to return a specific ticket_id
        receipt_push._receipt_outcomes = {ticket_id_1: "ok"}

        scheduler = Scheduler(
            session_factory=session_factory,
            push_client=receipt_push,
            settings=setup["scheduler"]._settings,
        )

        window = _make_window()
        local_date = WEEKDAY_DATE.isoformat()

        # Pre-insert a push_notification with a known ticket_id
        sent_time = window.start_utc
        with session_factory() as session:
            # Need the day to exist
            from chronos.models.devices import Device as DevModel
            from chronos.time import format_utc_iso

            device = session.get(DevModel, device_id)
            assert device is not None
            # Create day if needed
            day = session.exec(  # type: ignore[call-overload]
                select(Day).where(
                    Day.device_id == device_id,
                    Day.local_date == local_date,
                )
            ).first()
            if day is None:
                day = Day(
                    local_date=local_date,
                    device_id=device_id,
                    tz="America/Los_Angeles",
                    kind="weekday",
                    window_start_utc=format_utc_iso(window.start_utc),
                    window_end_utc=format_utc_iso(window.end_utc),
                    state=DayState.monitoring.value,
                    confirmed_at=None,
                    confirm_evaluation_id=None,
                    delivered_at=None,
                    fallback_used=0,
                    actual_wake_local=None,
                    ticks_sent=1,
                    ticks_received=0,
                    brief_id=None,
                    reel_id=None,
                    created_at=format_utc_iso(sent_time),
                    updated_at=format_utc_iso(sent_time),
                )
                session.add(day)
                session.flush()

            pn = PushNotification(
                device_id=device_id,
                kind=PushKind.tick.value,
                local_date=local_date,
                tick_seq=0,
                payload_json='{"type":"tick"}',
                ticket_id=ticket_id_1,
                ticket_status="ok",
                sent_at=format_utc_iso(sent_time),
                receipt_status=None,
            )
            session.add(pn)
            session.commit()

        # Run receipt check 16 min later
        check_time = sent_time + datetime.timedelta(minutes=16)
        await scheduler.run_receipts(check_time)

        # Receipt should be recorded
        with session_factory() as session:
            pn_updated = session.exec(  # type: ignore[call-overload]
                select(PushNotification).where(
                    PushNotification.ticket_id == ticket_id_1,
                )
            ).first()
            assert pn_updated is not None
            assert pn_updated.receipt_status == "ok"
            assert pn_updated.receipt_checked_at is not None


# ---------------------------------------------------------------------------
# anyio backend config
# ---------------------------------------------------------------------------

pytest_plugins = ("anyio",)
