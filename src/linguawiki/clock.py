"""Time primitives shared by contracts and later persistence layers."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class Clock(Protocol):
    """Injectable UTC clock."""

    def now(self) -> datetime: ...


class SystemClock:
    """Production clock returning timezone-aware UTC timestamps."""

    def now(self) -> datetime:
        return datetime.now(UTC)


def require_utc(value: datetime) -> datetime:
    """Require the canonical UTC timezone, not a date-dependent zero offset."""

    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    if value.utcoffset() != timedelta(0) or value.tzname() != "UTC":
        raise ValueError("timestamp must use UTC")
    return value.astimezone(UTC)


def require_utc_if_set(value: datetime | None) -> datetime | None:
    """Apply the UTC rule to an optional timestamp, and leave absence alone.

    An optional field is absent or a UTC timestamp; there is no third case. This exists
    because `require_utc` on an optional field only looks safe while the field is
    *omitted*: pydantic does not validate a default, so the rule never ran -- until the
    same payload came back with an explicit `null` in it, and then it raised
    `AttributeError` instead of validating anything.
    """

    return None if value is None else require_utc(value)


def validate_iana_timezone(value: str) -> str:
    """Return a valid IANA timezone name."""

    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown IANA timezone: {value}") from exc
    return value


def naive_utc(value: datetime) -> datetime:
    """Convert an aware UTC timestamp into the naive UTC form stored in DuckDB."""

    return require_utc(value).replace(tzinfo=None)


def aware_utc(value: datetime) -> datetime:
    """Interpret a naive stored timestamp as UTC."""

    if value.tzinfo is not None:
        return require_utc(value)
    return value.replace(tzinfo=UTC)
