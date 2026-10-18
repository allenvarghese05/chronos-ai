"""ULID generation for Chronos-AI.

ULIDs are generated app-side and stored as plain ``TEXT`` columns.
Use :func:`new_ulid` wherever a public identifier is required (e.g.
``devices.public_id``).  Never generate ULIDs inside a migration; only the
application generates them.
"""

from __future__ import annotations

from ulid import ULID


def new_ulid() -> str:
    """Return a new ULID as an uppercase 26-character string.

    Example::

        >>> new_ulid()
        '01ARYZ6S41TSV4RRFFQ69G5FAV'

    Returns:
        A 26-character Crockford base-32 string (uppercase).
    """
    return str(ULID())
