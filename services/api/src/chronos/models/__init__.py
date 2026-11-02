"""Chronos-AI SQLModel table classes.

Importing this package registers all table classes with ``SQLModel.metadata``
so that Alembic ``autogenerate`` sees the full schema.  Always import this
package (or ``from chronos.models import *``) before running migrations or
calling ``SQLModel.metadata.create_all()``.
"""

from __future__ import annotations

from chronos.models.content import Brief as Brief
from chronos.models.content import BriefItem as BriefItem
from chronos.models.content import Item as Item
from chronos.models.content import Source as Source
from chronos.models.delivery import PushNotification as PushNotification
from chronos.models.devices import Device as Device
from chronos.models.devices import PairingCode as PairingCode
from chronos.models.jobs import Job as Job
from chronos.models.settings import Setting as Setting
from chronos.models.wake import Day as Day
from chronos.models.wake import DayTransition as DayTransition
from chronos.models.wake import RuleVersion as RuleVersion
from chronos.models.wake import TelemetryEvent as TelemetryEvent
from chronos.models.wake import WakeEvaluation as WakeEvaluation

__all__ = [
    "Brief",
    "BriefItem",
    "Day",
    "DayTransition",
    "Device",
    "Item",
    "Job",
    "PairingCode",
    "PushNotification",
    "RuleVersion",
    "Setting",
    "Source",
    "TelemetryEvent",
    "WakeEvaluation",
]
