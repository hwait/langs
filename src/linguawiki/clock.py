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


def validate_iana_timezone(value: str) -> str:
    """Return a valid IANA timezone name."""

    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"unknown IANA timezone: {value}") from exc
    return value
