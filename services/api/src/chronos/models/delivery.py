"""SQLModel table class for push_notifications.

Source: docs/04-data-model.md §2.5.

Note: ``reel_id`` has no FK constraint referencing the ``reels`` table because
that table does not exist until T4.1.  The column is nullable and the FK will
be added in the media migration (T4.1). This is documented so reviewers do not add
it now.
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Index, UniqueConstraint
from sqlmodel import Field, SQLModel

from chronos.enums import PushKind, TicketStatus, ck_values


class PushNotification(SQLModel, table=True):
    """One outbound push notification sent to an Expo device token.

    Columns
    -------
    id               : INTEGER PK (autoincrement).
    device_id        : FK → devices.id.
    kind             : 'tick' | 'reel_ready' | 'text_fallback' | 'test'.
    local_date       : The local calendar date this push belongs to (YYYY-MM-DD).
    reel_id          : Nullable; FK to reels deferred to T4.1 (no constraint here).
    tick_seq         : Sequence number of the tick (0-based); NULL for non-tick kinds.
    payload_json     : What was sent (without the device token).
    ticket_id        : Expo ticket ID from push/send; NULL if the send failed before
                       Expo responded or status was error.
    ticket_status    : 'ok' | 'error'.
    receipt_status   : NULL until checked ≥ 15 min later; 'ok' or an Expo error code.
    sent_at          : UTC ISO-8601 timestamp when sent.
    receipt_checked_at : UTC ISO-8601 timestamp when the receipt was last checked; NULL
                       until the first receipt check.
    error            : Expo error message (if ticket_status='error'); never the token.
    """

    __tablename__ = "push_notifications"  # type: ignore[assignment]
    __table_args__ = (
        ForeignKeyConstraint(
            ["device_id"],
            ["devices.id"],
            name="fk_push_notifications_device_id",
        ),
        Index("ix_push_notifications_device_local_date", "device_id", "local_date"),
        Index("ix_push_notifications_kind", "kind"),
        # ticket_id is NULL on error; only non-NULL values must be unique.
        UniqueConstraint("ticket_id", name="uq_push_notifications_ticket_id"),
        CheckConstraint(
            f"kind IN {ck_values(PushKind)}",
            name="ck_push_notifications_kind",
        ),
        CheckConstraint(
            f"ticket_status IN {ck_values(TicketStatus)}",
            name="ck_push_notifications_ticket_status",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    device_id: int = Field()
    kind: str  # PushKind
    local_date: str  # "YYYY-MM-DD"
    # reel_id: FK to reels deferred to T4.1 (no FK constraint until that migration).
    reel_id: int | None = Field(default=None)
    tick_seq: int | None = Field(default=None)
    payload_json: str  # what was sent, without the device token
    ticket_id: str | None = Field(default=None)
    ticket_status: str  # TicketStatus
    receipt_status: str | None = Field(default=None)  # ReceiptStatus value or None
    sent_at: str
    receipt_checked_at: str | None = Field(default=None)
    error: str | None = Field(default=None)
