"""Condition parser for wake-engine penalty ``when`` clauses (T1.4).

A condition is one entry in a ``penalty.when`` dict, e.g.::

    stationary_sec_10m: ">= 540"
    steps_10m: "< 10"
    pedometer_auth: ["denied", "restricted"]   # "any of"

Supported operators: ``>=``, ``>``, ``<=``, ``<``, ``==``, ``in``.

A list value (``["denied", "restricted"]``) is always treated as an ``in``
membership test.  A bare string value (``">= 540"``) must begin with a
recognised operator.

The :class:`Condition` dataclass holds the parsed operator and operand, and
exposes :meth:`Condition.evaluate` for use by the scoring engine (T1.5).

Public API
----------
- :class:`Operator` — supported comparison operators.
- :class:`Condition` — parsed condition: operator + operand.
- :func:`parse_condition` — parse one raw ``when`` value.
- :func:`evaluate_condition` — convenience wrapper (parse + evaluate in one call).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class Operator(enum.StrEnum):
    """Supported comparison operators for wake-engine conditions."""

    gte = ">="
    gt = ">"
    lte = "<="
    lt = "<"
    eq = "=="
    in_ = "in"


# Longest operators first so that ">=" is tried before ">", etc.
_OPERATOR_ORDER: list[Operator] = [
    Operator.gte,
    Operator.lte,
    Operator.gt,
    Operator.lt,
    Operator.eq,
]


def _coerce(raw: str) -> bool | int | float | str:
    """Coerce a token string to its most natural Python type."""
    low = raw.strip().lower()
    if low == "true":
        return True
    if low == "false":
        return False
    try:
        int_val = int(raw.strip())
        return int_val
    except ValueError:
        pass
    try:
        float_val = float(raw.strip())
        return float_val
    except ValueError:
        pass
    return raw.strip()


@dataclass(frozen=True)
class Condition:
    """Parsed wake-engine condition.

    Attributes:
        operator:  The comparison operator.
        operand:   The right-hand side value (bool, int, float, str, or frozenset[str]).
    """

    operator: Operator
    operand: bool | int | float | str | frozenset[str]

    def evaluate(self, value: bool | int | float | str) -> bool:
        """Return ``True`` if *value* satisfies this condition.

        Args:
            value: The runtime signal value to test.

        Returns:
            Whether the condition is satisfied.

        Raises:
            TypeError: If the operator is not applicable to the types involved.
        """
        op = self.operator
        if op is Operator.in_:
            if not isinstance(self.operand, frozenset):
                raise TypeError("'in' operand must be a frozenset")
            return str(value) in self.operand

        # Scalar comparisons.
        rhs = self.operand
        if op is Operator.eq:
            # Allow cross-type equality for booleans sent as strings.
            if isinstance(rhs, bool) and isinstance(value, str):
                return str(rhs).lower() == value.lower()
            if isinstance(value, bool) and isinstance(rhs, str):
                return str(value).lower() == rhs.lower()
            return value == rhs  # type: ignore[operator]

        # Numeric comparisons — require numeric types on both sides.
        if not isinstance(value, (int, float)) or not isinstance(rhs, (int, float)):
            raise TypeError(
                f"Operator '{op}' requires numeric operands; "
                f"got value={type(value).__name__!r}, operand={type(rhs).__name__!r}"
            )
        if op is Operator.gte:
            return value >= rhs  # type: ignore[operator]
        if op is Operator.gt:
            return value > rhs  # type: ignore[operator]
        if op is Operator.lte:
            return value <= rhs  # type: ignore[operator]
        if op is Operator.lt:
            return value < rhs  # type: ignore[operator]

        raise TypeError(f"Unhandled operator: {op!r}")  # pragma: no cover


def parse_condition(raw: str | list[str]) -> Condition:
    """Parse a ``when`` value into a :class:`Condition`.

    Args:
        raw: Either a string like ``">= 540"`` / ``"< 10"`` / ``"== foreground"``
             or a list of strings like ``["denied", "restricted"]``.

    Returns:
        A :class:`Condition` ready for :meth:`~Condition.evaluate`.

    Raises:
        ValueError: If the string does not start with a recognised operator,
                    or if the list is empty, or if the operand cannot be parsed.
    """
    if isinstance(raw, list):
        if not raw:
            raise ValueError("Condition list must not be empty")
        return Condition(operator=Operator.in_, operand=frozenset(str(v) for v in raw))

    raw_str = raw.strip()
    for op in _OPERATOR_ORDER:
        if raw_str.startswith(op.value):
            rest = raw_str[len(op.value) :].strip()
            if not rest:
                raise ValueError(
                    f"Condition '{raw_str}' has no operand after operator '{op.value}'"
                )
            coerced = _coerce(rest)
            # Numeric comparison operators require a numeric operand.
            if op in (Operator.gte, Operator.gt, Operator.lte, Operator.lt):
                if not isinstance(coerced, (int, float)) or isinstance(coerced, bool):
                    raise ValueError(
                        f"Condition '{raw_str}': operator '{op.value}' requires a "
                        f"numeric operand; got {rest!r}"
                    )
            return Condition(operator=op, operand=coerced)

    raise ValueError(
        f"Invalid condition '{raw_str}'; must begin with one of "
        f"{[o.value for o in _OPERATOR_ORDER]} or be a list (for 'in')"
    )


def evaluate_condition(raw: str | list[str], value: bool | int | float | str) -> bool:
    """Parse *raw* and immediately evaluate it against *value*.

    Convenience wrapper for callers that do not need to cache the parsed
    :class:`Condition`.

    Args:
        raw:   The raw condition string or list.
        value: The runtime signal value.

    Returns:
        Whether the condition is satisfied.
    """
    return parse_condition(raw).evaluate(value)
