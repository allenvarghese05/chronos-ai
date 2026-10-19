"""Wake-engine rules schema, YAML loader, canonical JSON, DB versioning (T1.4).

This module owns the complete lifecycle of ``WakeRules``:

- **Schema validation** (Pydantic, ``extra="forbid"``):
  ``WakeRulesWindow``, ``Thresholds``, ``BonusConsecutive``, ``Rule``,
  ``Penalty``, ``WakeRules``.
- **YAML loader** — ``load_yaml(path) -> WakeRules`` (``yaml.safe_load`` only).
- **Canonical JSON + SHA-256** — deterministic; used for de-dup and replay.
- **In-memory cache** — ``RulesCache`` (thread-safe module-level singleton).
- **DB versioning** — ``activate_new_version``, ``ensure_initial_version``,
  ``yaml_stale``.

All cross-field validators are listed in docs/plans/T1.4-plan.md §4 step 5.

CORRECTNESS: ``activate_new_version`` is an explicit transaction.
A failing insert leaves the previous active version unchanged.

Public API
----------
- :class:`WakeRulesWindow`
- :class:`Thresholds`
- :class:`BonusConsecutive`
- :class:`Rule`
- :class:`Penalty`
- :class:`WakeRules`
- :func:`load_yaml`
- :func:`canonical_json`
- :func:`rules_sha256`
- :class:`RulesCache`
- :data:`_cache` — module-level singleton; use via :func:`get_effective_rules`.
- :func:`activate_new_version`
- :func:`ensure_initial_version`
- :func:`yaml_stale`
- :func:`reset_rules_cache` — test helper.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import logging
import threading
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text
from sqlmodel import Session, select

from chronos.models.wake import RuleVersion
from chronos.time import utc_now_iso
from chronos.wake.conditions import parse_condition
from chronos.wake.signals import SIGNALS, SignalKind
from chronos.wake.window import WindowConfig

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _time_from_hhmm(s: str) -> datetime.time:
    """Parse ``"HH:MM"`` to :class:`datetime.time`; raise ``ValueError`` on bad format."""
    try:
        return datetime.time.fromisoformat(s)
    except ValueError as exc:
        raise ValueError(f"Expected HH:MM time string, got {s!r}") from exc


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class WakeRulesWindow(BaseModel):
    """Window section of ``wake_rules.yaml``.

    Parses the nested ``weekday: {start, end}`` / ``weekend: {start, end}``
    shape from the YAML file and exposes :meth:`to_window_config` for the
    scoring engine.

    ``holidays_as_weekend`` is a list of ISO-8601 date strings (e.g.
    ``"2026-12-25"``); invalid dates raise a ``ValueError``.
    """

    model_config = ConfigDict(extra="forbid")

    weekday: dict[str, str]
    weekend: dict[str, str]
    day_boundary: str = "03:00"
    holidays_as_weekend: list[str] = []
    tick_interval_min: int = Field(default=5, ge=2, le=30)

    @model_validator(mode="after")
    def _validate_window(self) -> WakeRulesWindow:
        # Validate start/end keys present and parseable.
        for kind, d in (("weekday", self.weekday), ("weekend", self.weekend)):
            if "start" not in d or "end" not in d:
                raise ValueError(f"{kind} must have 'start' and 'end' keys")
            start = _time_from_hhmm(d["start"])
            end = _time_from_hhmm(d["end"])
            if start >= end:
                raise ValueError(f"{kind} start ({d['start']}) must be < {kind} end ({d['end']})")

        # Validate day_boundary parseable.
        _time_from_hhmm(self.day_boundary)

        # Validate holiday dates.
        for i, ds in enumerate(self.holidays_as_weekend):
            try:
                datetime.date.fromisoformat(ds)
            except ValueError as exc:
                raise ValueError(f"holidays_as_weekend[{i}]: invalid ISO date {ds!r}") from exc

        return self

    def to_window_config(self) -> WindowConfig:
        """Convert to the :class:`~chronos.wake.window.WindowConfig` used by scoring."""
        holidays: frozenset[datetime.date] = frozenset(
            datetime.date.fromisoformat(ds) for ds in self.holidays_as_weekend
        )
        return WindowConfig(
            weekday_start=_time_from_hhmm(self.weekday["start"]),
            weekday_end=_time_from_hhmm(self.weekday["end"]),
            weekend_start=_time_from_hhmm(self.weekend["start"]),
            weekend_end=_time_from_hhmm(self.weekend["end"]),
            day_boundary=_time_from_hhmm(self.day_boundary),
            holidays=holidays,
            tick_interval_min=self.tick_interval_min,
        )


class Thresholds(BaseModel):
    """Scoring threshold configuration."""

    model_config = ConfigDict(extra="forbid")

    confirm: int = Field(ge=1, le=100)
    strong: int = Field(ge=1, le=100)
    dedupe_min: int = Field(ge=1, le=60)
    confirm_max_gap_min: int = Field(default=12, ge=2, le=60)
    """Two consecutive ticks >= ``confirm`` must be <= this many minutes apart
    for the consecutive rule to fire.  Allows one missed tick at the default 5-min
    interval (gap = 10 min < 12).  Bounds: [2, 60]."""

    @model_validator(mode="after")
    def _check_strong_lte_confirm(self) -> Thresholds:
        if self.strong < self.confirm:
            raise ValueError(
                f"strong ({self.strong}) must be >= confirm ({self.confirm}); "
                "strong is the single-tick threshold and should be harder to reach"
            )
        return self


class BonusConsecutive(BaseModel):
    """Consecutive-tick bonus configuration."""

    model_config = ConfigDict(extra="forbid")

    ticks: int = Field(ge=2)
    points: int = Field(ge=0)


# Type alias for a points row (threshold, award).
# YAML delivers [[35, 30], [20, 18], ...]; after Pydantic coercion each inner
# item is a list of two elements.  We use list[list[...]] during parsing and
# validate the elements ourselves.
_PointsRowRaw = list[int | float | bool | str]


class Rule(BaseModel):
    """One scoring rule.

    ``points`` is a list of ``[threshold, points]`` rows; the first row
    whose threshold is satisfied awards its points.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    signal: str
    points: list[_PointsRowRaw]
    cap: int = Field(ge=0)
    requires: dict[str, list[str]] | None = None
    bonus_consecutive: BonusConsecutive | None = None

    @model_validator(mode="after")
    def _validate_rule(self) -> Rule:
        # 1. Signal must be in the registry.
        if self.signal not in SIGNALS:
            valid = sorted(SIGNALS.keys())
            raise ValueError(f"unknown signal {self.signal!r}; valid signals are {valid}")

        spec = SIGNALS[self.signal]

        # 2. Validate points rows have exactly 2 elements each.
        for i, row in enumerate(self.points):
            if not isinstance(row, list) or len(row) != 2:  # type: ignore[arg-type]
                raise ValueError(f"points[{i}] must be a list of [threshold, points]; got {row!r}")

        # 3. Signal-kind-specific validations.
        thresholds = [row[0] for row in self.points]
        point_values = [row[1] for row in self.points]

        if spec.kind is SignalKind.bool_:
            if len(self.points) != 1:
                raise ValueError(
                    f"bool signals must have exactly one threshold row; got {len(self.points)}"
                )
            if not isinstance(thresholds[0], bool):
                raise ValueError(
                    f"bool signal threshold must be true or false; got {thresholds[0]!r}"
                )

        elif spec.kind is SignalKind.numeric:
            # All thresholds must be numeric.  Build a narrowed list so the
            # descending-order comparison is type-safe (int | float >= int | float).
            numeric_thresholds: list[int | float] = []
            for i, t in enumerate(thresholds):
                if not isinstance(t, (int, float)) or isinstance(t, bool):
                    raise ValueError(
                        f"numeric signal threshold must be a number; got {t!r} at points[{i}]"
                    )
                numeric_thresholds.append(t)
            # Strictly descending.
            for i in range(1, len(numeric_thresholds)):
                if numeric_thresholds[i] >= numeric_thresholds[i - 1]:
                    raise ValueError(
                        f"thresholds must be in strictly descending order; got {thresholds!r}"
                    )

        elif spec.kind is SignalKind.enum_:
            if spec.enum_values:
                # enum with known values: validate each threshold.
                for _i, t in enumerate(thresholds):
                    if str(t) not in spec.enum_values:
                        raise ValueError(
                            f"enum signal {self.signal!r} threshold {t!r} not in "
                            f"allowed values {sorted(spec.enum_values)}"
                        )
            # Bool signals stored in the YAML as booleans are handled by bool_ kind.
            # Enums with empty enum_values (device_ts, tz, app_version, ios_version)
            # allow any string threshold.

        # 4. cap >= max points in points rows.
        if point_values:
            max_pts = max(int(p) for p in point_values)
            if self.cap < max_pts:
                raise ValueError(f"cap ({self.cap}) must be >= max point threshold ({max_pts})")

        return self


class Penalty(BaseModel):
    """One scoring penalty."""

    model_config = ConfigDict(extra="forbid")

    id: str
    when: dict[str, str | list[str]]
    points: int = Field(le=0)  # 0 = mark only; negative = deduct

    @model_validator(mode="after")
    def _validate_conditions(self) -> Penalty:
        for signal_name, raw in self.when.items():
            # Signal must be in registry.
            if signal_name not in SIGNALS:
                valid = sorted(SIGNALS.keys())
                raise ValueError(
                    f"penalty when: unknown signal {signal_name!r}; valid signals are {valid}"
                )
            # Condition must parse.
            try:
                parse_condition(raw)
            except ValueError as exc:
                raise ValueError(f"penalty when[{signal_name!r}]: {exc}") from exc
        return self


class WakeRules(BaseModel):
    """Complete wake-engine rules document.

    ``version`` is the schema/document version (integer, monotonically
    increasing in the DB).  It is **not** the DB ``RuleVersion.version``
    column — that is assigned by :func:`activate_new_version`.
    """

    model_config = ConfigDict(extra="forbid")

    version: int
    window: WakeRulesWindow
    thresholds: Thresholds
    rules: list[Rule]
    penalties: list[Penalty] = []
    reweight_without_pedometer: dict[str, int] = {}

    @model_validator(mode="after")
    def _validate_wake_rules(self) -> WakeRules:
        # 1. Non-empty rules list.
        if not self.rules:
            raise ValueError("rules list must not be empty")

        # 2. Unique rule IDs.
        seen_ids: dict[str, int] = {}
        for i, rule in enumerate(self.rules):
            if rule.id in seen_ids:
                raise ValueError(
                    f"duplicate rule id {rule.id!r} at index {i} "
                    f"(first seen at index {seen_ids[rule.id]})"
                )
            seen_ids[rule.id] = i

        # 3. reweight keys ⊆ rule ids.
        rule_ids = set(seen_ids.keys())
        for key in self.reweight_without_pedometer:
            if key not in rule_ids:
                raise ValueError(
                    f"reweight_without_pedometer key {key!r} is not a valid rule id; "
                    f"valid ids are {sorted(rule_ids)}"
                )

        # 4. Reweighted sum ≤ 100.
        if self.reweight_without_pedometer:
            total = sum(self.reweight_without_pedometer.values())
            if total > 100:
                raise ValueError(f"reweight_without_pedometer values sum to {total}; must be ≤ 100")

        return self


# ---------------------------------------------------------------------------
# YAML loader
# ---------------------------------------------------------------------------


def load_yaml(path: Path) -> WakeRules:
    """Load and validate ``wake_rules.yaml`` from *path*.

    Args:
        path: Path to the YAML file (typically ``config/wake_rules.yaml``).

    Returns:
        A validated :class:`WakeRules` instance.

    Raises:
        :class:`yaml.YAMLError`: On YAML parse errors.
        :class:`pydantic.ValidationError`: On schema validation errors.
    """
    with path.open() as fh:
        raw: Any = yaml.safe_load(fh)
    return WakeRules.model_validate(raw)


# ---------------------------------------------------------------------------
# Canonical JSON + SHA-256
# ---------------------------------------------------------------------------


def canonical_json(rules: WakeRules) -> str:
    """Return deterministic JSON for SHA-256 computation.

    Uses ``model_dump(mode="json")`` (converts ``datetime.time`` to ISO strings)
    then ``json.dumps`` with ``sort_keys=True`` and no extra whitespace.

    The output is stable across Python versions for the same rule content.
    """
    return json.dumps(rules.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


def rules_sha256(rules: WakeRules) -> str:
    """Return the SHA-256 hex digest of :func:`canonical_json`."""
    return hashlib.sha256(canonical_json(rules).encode()).hexdigest()


# ---------------------------------------------------------------------------
# In-memory cache
# ---------------------------------------------------------------------------


class RulesCache:
    """Thread-safe cache of the currently active ``(WakeRules, RuleVersion)``.

    ``get_effective`` returns the cached pair; on a cache miss it fetches the
    active row from the DB and validates the stored JSON.

    ``invalidate`` clears the cache.  Called by :func:`activate_new_version`
    and ``chronos config apply-yaml``.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._rules: WakeRules | None = None
        self._rv: RuleVersion | None = None

    def get_effective(self, session: Session) -> tuple[WakeRules, RuleVersion]:
        """Return ``(WakeRules, RuleVersion)`` for the active version.

        Reloads from DB if the cache is empty.

        Raises:
            RuntimeError: If no active rule version is found in the DB.
        """
        with self._lock:
            if self._rules is not None and self._rv is not None:
                return self._rules, self._rv

            stmt = select(RuleVersion).where(RuleVersion.active == 1)
            rv = session.exec(stmt).first()  # type: ignore[call-overload]
            if rv is None:
                raise RuntimeError(
                    "No active rule version found in DB. Call ensure_initial_version() on startup."
                )
            rules = WakeRules.model_validate_json(rv.rules_json)
            self._rules = rules
            self._rv = rv
            return rules, rv

    def invalidate(self) -> None:
        """Clear the cache so the next call to :meth:`get_effective` reloads from DB."""
        with self._lock:
            self._rules = None
            self._rv = None


# Module-level singleton.
_cache: RulesCache = RulesCache()


def get_rules_cache() -> RulesCache:
    """Return the module-level :class:`RulesCache` singleton.

    Prefer :func:`reset_rules_cache` for test teardown and
    :meth:`RulesCache.get_effective` (via this accessor) for reads.
    """
    return _cache


def reset_rules_cache() -> None:
    """Reset the module-level cache. Called by ``conftest._reset_globals`` between tests."""
    _cache.invalidate()


# ---------------------------------------------------------------------------
# DB versioning
# ---------------------------------------------------------------------------


def activate_new_version(
    session: Session,
    rules: WakeRules,
    source: str,
    created_by: str,
) -> tuple[RuleVersion, bool]:
    """Atomically deactivate the current version and insert a new active one.

    If the canonical JSON SHA of *rules* is identical to the active version's
    SHA, returns the existing ``(RuleVersion, False)`` — no DB write.

    The operation is an explicit transaction:

    1. ``SELECT MAX(version)`` from ``rule_versions``.
    2. Check if SHA matches active — early return if so.
    3. ``UPDATE rule_versions SET active=0 WHERE active=1``.
    4. ``INSERT`` new row with ``active=1`` and ``version = max+1``.

    If step 4 raises, the transaction is rolled back; the previously-active
    row remains ``active=1``.

    Args:
        session:    A :class:`~sqlmodel.Session` (will ``begin()`` a savepoint).
        rules:      The :class:`WakeRules` to activate.
        source:     ``"yaml"`` or ``"api"``.
        created_by: ``"startup"`` / ``"cli"`` / ``"api:<device_public_id>"``.

    Returns:
        ``(RuleVersion, created)`` where *created* is ``False`` for a SHA-match no-op.
    """
    sha = rules_sha256(rules)
    cj = canonical_json(rules)
    now = utc_now_iso()

    with session.no_autoflush, session.begin_nested():
        # Check current active version (autoflush suppressed to avoid side effects
        # from implicit flushes inside the savepoint).
        active_stmt = select(RuleVersion).where(RuleVersion.active == 1)
        active_rv = session.exec(active_stmt).first()  # type: ignore[call-overload]

        if active_rv is not None and active_rv.sha256 == sha:
            # Identical rules — no-op.
            return active_rv, False

        # Get next version number.
        all_rvs = session.exec(select(RuleVersion)).all()  # type: ignore[call-overload]
        next_version = (max(rv.version for rv in all_rvs) + 1) if all_rvs else 1

        # Deactivate current (if any) — flush separately so a failing INSERT
        # does not prevent this UPDATE from being included in the savepoint rollback.
        if active_rv is not None:
            active_rv.active = 0
            session.add(active_rv)
            session.flush()  # flush the deactivation UPDATE

        # Insert new active version — second explicit flush; callers may monkeypatch
        # session.flush to simulate a DB failure here.
        new_rv = RuleVersion(
            version=next_version,
            source=source,
            rules_json=cj,
            sha256=sha,
            created_at=now,
            created_by=created_by,
            active=1,
        )
        session.add(new_rv)
        session.flush()  # flush the insertion — if this raises, savepoint rolls back both

    # Invalidate cache after successful commit.
    _cache.invalidate()
    return new_rv, True


def ensure_initial_version(session: Session, yaml_path: Path) -> RuleVersion:
    """Ensure at least one active rule version exists; insert v1 from YAML if not.

    Idempotent: if an active version already exists, returns it unchanged.
    If no active version exists, loads *yaml_path*, validates, and inserts
    version 1 with ``source="yaml"`` and ``created_by="startup"``.

    Args:
        session:   An open :class:`~sqlmodel.Session`.
        yaml_path: Path to ``wake_rules.yaml``.

    Returns:
        The (possibly newly created) active :class:`RuleVersion`.

    Raises:
        :class:`yaml.YAMLError` / :class:`pydantic.ValidationError`:
            If the YAML file is missing or invalid. These propagate to the
            startup caller, which should exit non-zero.
    """
    stmt = select(RuleVersion).where(RuleVersion.active == 1)
    active_rv = session.exec(stmt).first()  # type: ignore[call-overload]
    if active_rv is not None:
        return active_rv

    log.info("No active rule version; loading from %s", yaml_path)
    rules = load_yaml(yaml_path)
    rv, _ = activate_new_version(session, rules, source="yaml", created_by="startup")
    session.commit()
    log.info("Inserted rule version %d from %s", rv.version, yaml_path)
    return rv


def yaml_stale(session: Session, yaml_path: Path) -> bool:
    """Return ``True`` if the YAML file has changed since the last yaml-sourced version.

    "Stale" means: the SHA-256 of the current YAML file content differs from
    the ``sha256`` column of the most-recent ``source='yaml'`` row in
    ``rule_versions``.

    Returns ``False`` if no yaml-sourced version exists (nothing to compare to).
    """
    # Load current YAML sha.
    try:
        current_rules = load_yaml(yaml_path)
    except Exception:
        return True  # Can't load YAML → treat as stale.
    current_sha = rules_sha256(current_rules)

    # Find the latest yaml-sourced version.
    stmt = (
        select(RuleVersion).where(text("source = 'yaml'")).order_by(RuleVersion.version.desc())  # type: ignore[attr-defined]
    )
    latest_yaml_rv = session.exec(stmt).first()  # type: ignore[call-overload]
    if latest_yaml_rv is None:
        return False

    return current_sha != latest_yaml_rv.sha256
