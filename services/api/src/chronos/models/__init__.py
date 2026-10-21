"""Chronos-AI SQLModel table classes.

Importing this package registers all table classes with ``SQLModel.metadata``
so that Alembic ``autogenerate`` sees the full schema.  Always import this
package (or ``from chronos.models import *``) before running migrations or
calling ``SQLModel.metadata.create_all()``.
"""

from __future__ import annotations

from chronos.models.delivery import PushNotification as PushNotification
from chronos.models.devices import Device as Device
from chronos.models.devices import PairingCode as PairingCode
from chronos.models.settings import Setting as Setting
from chronos.models.wake import Day as Day
from chronos.models.wake import DayTransition as DayTransition
from chronos.models.wake import RuleVersion as RuleVersion
from chronos.models.wake import TelemetryEvent as TelemetryEvent
from chronos.models.wake import WakeEvaluation as WakeEvaluation

__all__ = [
    "Day",
    "DayTransition",
    "Device",
    "PairingCode",
    "PushNotification",
    "RuleVersion",
    "Setting",
    "TelemetryEvent",
    "WakeEvaluation",
]
