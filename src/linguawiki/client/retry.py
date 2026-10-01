"""Bounded retry for the two refusals that mean "somebody else has the file".

DuckDB holds an exclusive lock on the database and the application lock is non-blocking, so
a learner running one CLI command makes the server's next read fail. That is normal rather
than exceptional, and the correct response is to try again a few times and then say so.

Retrying is decided by the error's own `retryable` flag, never by a list of codes here: a
list in a second place is a list that drifts, and a new retryable refusal would be surfaced
immediately by a server that kept its own copy.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from linguawiki.errors import LinguaWikiError

#: Four attempts over roughly a third of a second. Long enough to ride out a short command
#: holding the writer, short enough that a page is told rather than left hanging -- a
#: `database_busy` the server waits on silently is the hang the honest state exists to avoid.
DEFAULT_ATTEMPTS = 4
DEFAULT_BASE_DELAY = 0.05


def with_retry[T](
    operation: Callable[[], T],
    *,
    attempts: int = DEFAULT_ATTEMPTS,
    base_delay: float = DEFAULT_BASE_DELAY,
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """Run `operation`, retrying a retryable refusal, and re-raise the last one.

    The last error, not the first: it is the one that describes the state the caller is
    actually in, and the first is already over. `sleep` is injected so a test can exhaust
    the budget without spending the wall clock on it.
    """

    delay = base_delay
    for attempt in range(1, attempts + 1):
        try:
            return operation()
        except LinguaWikiError as failure:
            if not failure.payload.retryable or attempt == attempts:
                raise
            sleep(delay)
            delay *= 2
    # Unreachable: the loop either returns or raises. Kept explicit rather than relying on
    # a fall-through that would silently return `None` if the bounds above ever changed.
    raise AssertionError("retry loop neither returned nor raised")


__all__ = ["DEFAULT_ATTEMPTS", "DEFAULT_BASE_DELAY", "with_retry"]
