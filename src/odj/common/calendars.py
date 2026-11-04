"""NYSE trading calendar (holidays + early closes) and snapshot-slot timing in New York time."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from typing import Any, cast

import pandas as pd
import pandas_market_calendars as mcal

from odj.common.timeutil import ET, et_datetime

REGULAR_CLOSE = "16:00"


@lru_cache(maxsize=1)
def _nyse() -> Any:
    return mcal.get_calendar("NYSE")


@lru_cache(maxsize=64)
def _schedule(start: date, end: date) -> pd.DataFrame:
    return cast(pd.DataFrame, _nyse().schedule(start_date=start, end_date=end))


@dataclass(frozen=True)
class Session:
    date: date
    open_et: datetime
    close_et: datetime

    @property
    def is_half_day(self) -> bool:
        return self.close_et.strftime("%H:%M") != REGULAR_CLOSE


def session_for(d: date) -> Session | None:
    """The trading session for calendar date d, or None if the market is closed."""
    sched = _schedule(d - timedelta(days=0), d)
    if sched.empty:
        return None
    row = sched.iloc[0]
    return Session(
        date=d,
        open_et=row["market_open"].to_pydatetime().astimezone(ET),
        close_et=row["market_close"].to_pydatetime().astimezone(ET),
    )


def is_trading_day(d: date) -> bool:
    return session_for(d) is not None


def is_half_day(d: date) -> bool:
    s = session_for(d)
    return bool(s and s.is_half_day)


def trading_days(start: date, end: date) -> list[date]:
    sched = _nyse().schedule(start_date=start, end_date=end)
    return [ts.date() for ts in sched.index]


def market_is_open(now: datetime) -> bool:
    s = session_for(now.astimezone(ET).date())
    return bool(s and s.open_et <= now <= s.close_et)


@dataclass(frozen=True)
class SlotTime:
    name: str
    date: date
    at_et: datetime  # effective slot time (after half-day clamp)

    @property
    def hhmm(self) -> str:
        return self.at_et.strftime("%H%M")


def slot_time(slot_cfg: dict[str, Any], d: date) -> SlotTime | None:
    """Effective ET time of a slot on date d (None on non-trading days).

    `clamp_to_close_minutes`: the slot is at most this many minutes before the session close,
    so the late slot lands at 12:50 on a 13:00 half-day instead of after the bell.
    """
    s = session_for(d)
    if s is None:
        return None
    at = et_datetime(d, str(slot_cfg["time"]))
    clamp = slot_cfg.get("clamp_to_close_minutes")
    if clamp is not None:
        at = min(at, s.close_et - timedelta(minutes=int(clamp)))
    return SlotTime(name=str(slot_cfg["name"]), date=d, at_et=at)


def in_slot_window(now: datetime, st: SlotTime, window_minutes: int) -> bool:
    return st.at_et <= now <= st.at_et + timedelta(minutes=window_minutes)


def match_slot(
    now: datetime, slots_cfg: list[dict[str, Any]], window_minutes: int
) -> SlotTime | None:
    """Which configured slot (if any) is `now` inside the window of? Evaluated in ET, DST-proof."""
    d = now.astimezone(ET).date()
    for sc in slots_cfg:
        st = slot_time(sc, d)
        if st is not None and in_slot_window(now, st, window_minutes):
            return st
    return None


def expected_slot_times(
    slots_cfg: list[dict[str, Any]], start: datetime, end: datetime
) -> list[SlotTime]:
    """All slot times falling in [start, end] (aware datetimes), honoring holidays/half-days."""
    out: list[SlotTime] = []
    d = start.astimezone(ET).date() - timedelta(days=1)
    last = end.astimezone(ET).date()
    while d <= last:
        for sc in slots_cfg:
            st = slot_time(sc, d)
            if st is not None and start <= st.at_et <= end:
                out.append(st)
        d += timedelta(days=1)
    return sorted(out, key=lambda s: s.at_et)
