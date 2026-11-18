"""Retrying a READ that failed for a reason that was not the request's fault.

Google answers UNAVAILABLE, DEADLINE_EXCEEDED and INTERNAL a few times a day
under normal load. For an agent in continuous use that turned into a failed
tool call it then had to reason about, for a request that would have worked a
second later.

READS ONLY, and that is the whole safety argument. A read retried is the same
read. A mutate retried after a timeout may be a mutate applied twice -- the
first can have landed at Google's end before the connection dropped -- so the
write path never retries; it reports TRANSIENT and lets a human look at
list_my_changes first.
"""

from __future__ import annotations

import time
from typing import Callable, TypeVar

from .errors import is_transient

T = TypeVar("T")

#: Attempts in total, including the first.
TRIES = 3
#: Seconds before the second attempt; doubled before each one after.
BASE_DELAY = 0.5

#: Indirected so tests can make the backoff instant.
sleep = time.sleep


def retry_read(fn: Callable[[], T], *, tries: int = TRIES, base_delay: float = BASE_DELAY) -> T:
    """``fn()``, retried with exponential backoff while it fails transiently."""
    for attempt in range(1, tries + 1):
        try:
            return fn()
        except Exception as exc:
            if attempt == tries or not is_transient(exc):
                raise
            sleep(base_delay * 2 ** (attempt - 1))
    raise AssertionError("unreachable")  # pragma: no cover
