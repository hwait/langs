"""Controllable clocks used instead of wall-clock time in tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


class FixedClock:
    """Always returns the same instant."""

    def __init__(self, moment: datetime = EPOCH) -> None:
        self._moment = moment

    def now(self) -> datetime:
        return self._moment


class AdvancingClock:
    """Advances one step per read so ordered and named artifacts stay distinct."""

    def __init__(self, start: datetime = EPOCH, step: timedelta = timedelta(seconds=1)) -> None:
        self._now = start
        self._step = step

    def now(self) -> datetime:
        self._now += self._step
        return self._now

    def advance(self, amount: timedelta) -> None:
        self._now += amount
