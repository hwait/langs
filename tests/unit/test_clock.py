from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from linguawiki.clock import require_utc, validate_iana_timezone


def test_utc_and_iana_timezone_validation() -> None:
    value = datetime(2026, 1, 1, tzinfo=UTC)

    assert require_utc(value) == value
    assert validate_iana_timezone("Asia/Tbilisi") == "Asia/Tbilisi"


@pytest.mark.parametrize(
    "value",
    [datetime(2026, 1, 1), datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=4)))],
)
def test_non_utc_timestamps_are_rejected(value: datetime) -> None:
    with pytest.raises(ValueError, match="UTC|timezone-aware"):
        require_utc(value)


def test_zero_offset_region_is_not_mistaken_for_utc() -> None:
    winter_in_london = datetime(2026, 1, 1, tzinfo=ZoneInfo("Europe/London"))
    assert winter_in_london.utcoffset() == timedelta(0)

    with pytest.raises(ValueError, match="must use UTC"):
        require_utc(winter_in_london)


def test_unknown_timezone_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown IANA timezone"):
        validate_iana_timezone("Mars/Olympus")
