"""Generic staleness logic, reusable for other bots.

Given the datetimes at which a job *should* have run and the datetimes at which it did succeed,
report which expected runs are overdue (older than `grace` and with no success at/after them).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta


def overdue(
    expected: Sequence[datetime],
    successes: Iterable[datetime],
    now: datetime,
    grace: timedelta,
    *,
    tolerance: timedelta = timedelta(minutes=5),
) -> list[datetime]:
    """Expected run times that are past due.

    A run expected at `t` is satisfied by any success in [t - tolerance, next_expected_time).
    If there is no later expected run, any success >= t - tolerance counts.
    """
    succ = sorted(successes)
    exp = sorted(expected)
    late: list[datetime] = []
    for i, t in enumerate(exp):
        if now < t + grace:
            continue
        upper = exp[i + 1] if i + 1 < len(exp) else None
        ok = any(s >= t - tolerance and (upper is None or s < upper - tolerance) for s in succ)
        if not ok:
            late.append(t)
    return late
