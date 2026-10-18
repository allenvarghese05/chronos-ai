"""SQLModel table classes: Device, PairingCode.

These are the auth and device-identity tables (§2.1 of docs/04-data-model.md).
Plain FK columns only — no Relationship() helpers (owner answer 1).
"""

from __future__ import annotations

from sqlalchemy import CheckConstraint, String, UniqueConstraint
from sqlalchemy import Column as SAColumn
from sqlmodel import Field, SQLModel

from chronos.enums import DeviceStatus, ck_values


class Device(SQLModel, table=True):
    """The paired iPhone.  v1: exactly one ``active`` row at a time."""

    __tablename__ = "devices"  # type: ignore[assignment]
    __table_args__ = (
        UniqueConstraint("public_id", name="uq_devices_public_id"),
        UniqueConstraint("token_hash", name="uq_devices_token_hash"),
        CheckConstraint(
            f"status IN {ck_values(DeviceStatus)}",
            name="ck_devices_status",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    public_id: str = Field(sa_column=SAColumn("public_id", String, nullable=False))
    name: str
    token_hash: str
    expo_push_token: str | None = Field(default=None)
    tz: str
    app_version: str | None = Field(default=None)
    ios_version: str | None = Field(default=None)
    model: str | None = Field(default=None)
    status: str  # DeviceStatus
    created_at: str
    last_seen_at: str
    revoked_at: str | None = Field(default=None)


class PairingCode(SQLModel, table=True):
    """Short-lived pairing codes shown by ``chronos pair-code``."""

    __tablename__ = "pairing_codes"  # type: ignore[assignment]
    __table_args__ = (UniqueConstraint("code_hash", name="uq_pairing_codes_code_hash"),)

    id: int | None = Field(default=None, primary_key=True)
    code_hash: str
    expires_at: str
    consumed_at: str | None = Field(default=None)
    attempts: int = 0
