"""Tiny retry-with-backoff helper (no external dependency)."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")
log = logging.getLogger("odj.retry")


def with_retries(
    fn: Callable[[], T],
    *,
    attempts: int = 3,
    base_delay: float = 2.0,
    sleep: Callable[[float], None] = time.sleep,
    what: str = "operation",
) -> T:
    last: Exception | None = None
    for i in range(1, attempts + 1):
        try:
            return fn()
        except (
            Exception
        ) as exc:  # transient provider failures are broad (HTTP, parsing, rate limit)
            last = exc
            if i == attempts:
                break
            delay = base_delay * (2 ** (i - 1))
            log.warning(
                "%s failed (attempt %d/%d): %s; retrying in %.1fs", what, i, attempts, exc, delay
            )
            sleep(delay)
    assert last is not None
    raise last
