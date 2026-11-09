"""Shared enum definitions for Chronos-AI.

Every enum here is a ``StrEnum`` (``str`` + ``enum.Enum``) so that:

- Pydantic v2 models accept enum members *and* their string values.
- SQLModel ``CheckConstraint`` builders can reference ``.value`` without
  extra conversion.
- Pyright strict mode is happy (``str`` mixin satisfies ``str`` annotations).

One definition per enum — used by both the SQLModel table classes (to build
``CheckConstraint`` lists) and the Pydantic request/response DTOs in T1.2+.
"""

from __future__ import annotations

import enum


class WakeSource(enum.StrEnum):
    """How the iOS app was woken to sample signals."""

    silent_push = "silent_push"
    bg_refresh = "bg_refresh"
    foreground = "foreground"
    manual = "manual"


class WalkingConf(enum.StrEnum):
    """CoreMotion walking-activity confidence level."""

    low = "low"
    medium = "medium"
    high = "high"


class BatteryState(enum.StrEnum):
    """iOS battery / charger state."""

    unplugged = "unplugged"
    charging = "charging"
    full = "full"
    unknown = "unknown"


class AppState(enum.StrEnum):
    """UIApplication application state."""

    active = "active"
    background = "background"
    inactive = "inactive"


class PedometerAuth(enum.StrEnum):
    """CMPedometer authorisation status."""

    authorized = "authorized"
    denied = "denied"
    restricted = "restricted"
    not_determined = "not_determined"


class DayKind(enum.StrEnum):
    """Calendar classification for a local date."""

    weekday = "weekday"
    weekend = "weekend"
    holiday = "holiday"


class DayState(enum.StrEnum):
    """State machine state for a ``days`` row."""

    idle = "idle"
    monitoring = "monitoring"
    confirmed = "confirmed"
    fallback = "fallback"
    delivered = "delivered"


class DeviceStatus(enum.StrEnum):
    """Lifecycle status of a paired device."""

    active = "active"
    revoked = "revoked"


class RuleSource(enum.StrEnum):
    """How a ``rule_versions`` row was created."""

    yaml = "yaml"
    api = "api"


class EvaluationGate(enum.StrEnum):
    """Whether the evaluation window was open."""

    open = "open"
    closed = "closed"


class EvaluationDecision(enum.StrEnum):
    """Outcome of a wake evaluation."""

    outside_window = "outside_window"
    low = "low"
    high = "high"
    confirm = "confirm"
    ignored_dedupe = "ignored_dedupe"
    already_confirmed = "already_confirmed"


class TransitionCause(enum.StrEnum):
    """What triggered a ``day_transitions`` row."""

    evaluation = "evaluation"
    scheduler_window_start = "scheduler_window_start"
    scheduler_window_end = "scheduler_window_end"
    deliver_ok = "deliver_ok"
    manual = "manual"


class PushKind(enum.StrEnum):
    """Kind of push notification sent to the device."""

    tick = "tick"
    reel_ready = "reel_ready"
    text_fallback = "text_fallback"
    test = "test"


class TicketStatus(enum.StrEnum):
    """Expo ticket status returned from push/send."""

    ok = "ok"
    error = "error"


class ReceiptStatus(enum.StrEnum):
    """Expo receipt status returned from push/getReceipts."""

    ok = "ok"
    device_not_registered = "DeviceNotRegistered"
    message_rate_exceeded = "MessageRateExceeded"
    message_too_big = "MessageTooBig"
    invalid_credentials = "InvalidCredentials"


class SourceKind(enum.StrEnum):
    """Supported future content adapters."""

    hf_api = "hf_api"
    arxiv_api = "arxiv_api"
    rss = "rss"
    hn_rss = "hn_rss"


class ContentCategory(enum.StrEnum):
    """Selection categories, shared by content storage and future loaders."""

    ai = "ai"
    tech = "tech"
    world = "world"
    india = "india"


class BriefStatus(enum.StrEnum):
    """A stored draft is not yet a validated ready script."""

    drafting = "drafting"
    ready = "ready"
    failed = "failed"


class BriefItemRole(enum.StrEnum):
    """An item's role in a future scene."""

    primary = "primary"
    mention = "mention"


class ReelStatus(enum.StrEnum):
    """Media pipeline progress for one reel (docs/04 §2.4)."""

    assets = "assets"
    tts = "tts"
    captions = "captions"
    rendering = "rendering"
    ready = "ready"
    failed = "failed"


class ReelAssetKind(enum.StrEnum):
    voice_wav = "voice_wav"
    voice_mp3 = "voice_mp3"
    captions_json = "captions_json"
    reel_json = "reel_json"
    image = "image"
    broll = "broll"
    thumbnail = "thumbnail"


class AssetLicence(enum.StrEnum):
    pexels = "pexels"
    og_image = "og_image"
    generated = "generated"
    kokoro = "kokoro"
    edge_tts = "edge_tts"


class JobKind(enum.StrEnum):
    """Trusted internal stage identifiers; not executable module names."""

    ingest = "ingest"
    script = "script"
    assets = "assets"
    tts = "tts"
    captions = "captions"
    render = "render"
    deliver = "deliver"
    receipts = "receipts"
    retention = "retention"
    report = "report"


class JobStatus(enum.StrEnum):
    """Durable execution states."""

    queued = "queued"
    running = "running"
    done = "done"
    failed = "failed"
    cancelled = "cancelled"


def ck_values(e: type[enum.Enum]) -> str:
    """Return a SQL IN-list string for use in a ``CheckConstraint``.

    Example::

        ck_values(WakeSource)
        # "('silent_push','bg_refresh','foreground','manual')"
    """
    values = ", ".join(f"'{m.value}'" for m in e)
    return f"({values})"
