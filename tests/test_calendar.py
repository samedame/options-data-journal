"""Holidays, weekends, half-days and DST handling (all evaluated in America/New_York)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from odj.common.calendars import (
    expected_slot_times,
    is_half_day,
    is_trading_day,
    match_slot,
    session_for,
    slot_time,
)
from odj.common.config import tickers_cfg

SLOTS = tickers_cfg()["snapshot"]["slots"]
WINDOW = int(tickers_cfg()["snapshot"]["slot_window_minutes"])
OPEN, CLOSE = SLOTS[0], SLOTS[1]


def test_trading_days() -> None:
    assert is_trading_day(date(2026, 11, 2))  # Monday
    assert not is_trading_day(date(2026, 11, 28))  # Saturday
    assert not is_trading_day(date(2026, 11, 29))  # Sunday
    assert not is_trading_day(date(2026, 11, 26))  # Thanksgiving
    assert not is_trading_day(date(2026, 7, 3))  # Independence Day observed (Jul 4 is a Saturday)
    assert not is_trading_day(date(2026, 12, 25))  # Christmas


def test_half_day_is_detected_and_late_slot_is_clamped() -> None:
    d = date(2026, 11, 27)  # day after Thanksgiving: 13:00 ET close
    assert is_half_day(d) and not is_half_day(date(2026, 11, 25))
    s = session_for(d)
    assert s is not None and s.close_et.strftime("%H:%M") == "13:00"
    assert slot_time(CLOSE, d) is not None
    assert slot_time(CLOSE, d).at_et.strftime("%H:%M") == "12:50"  # type: ignore[union-attr]
    assert slot_time(CLOSE, date(2026, 11, 25)).at_et.strftime("%H:%M") == "15:50"  # type: ignore[union-attr]
    assert slot_time(OPEN, d).at_et.strftime("%H:%M") == "09:45"  # type: ignore[union-attr]


def test_no_slots_on_closed_days() -> None:
    assert slot_time(OPEN, date(2026, 11, 28)) is None
    assert slot_time(CLOSE, date(2026, 11, 26)) is None


def test_dst_spring_forward_double_cron_exits_quietly() -> None:
    """2026-03-08 (Sunday) is the spring-forward date. Mon 03-09 is EDT, Fri 03-06 is EST."""
    # EDT: 09:45 ET == 13:45 UTC
    assert match_slot(datetime(2026, 3, 9, 13, 50, tzinfo=UTC), SLOTS, WINDOW).name == "open"  # type: ignore[union-attr]
    assert (
        match_slot(datetime(2026, 3, 9, 14, 50, tzinfo=UTC), SLOTS, WINDOW) is None
    )  # the "other" cron
    # EST: 09:45 ET == 14:45 UTC
    assert match_slot(datetime(2026, 3, 6, 13, 50, tzinfo=UTC), SLOTS, WINDOW) is None
    assert match_slot(datetime(2026, 3, 6, 14, 50, tzinfo=UTC), SLOTS, WINDOW).name == "open"  # type: ignore[union-attr]
    assert not is_trading_day(date(2026, 3, 8))


def test_dst_fall_back_double_cron_exits_quietly() -> None:
    """2026-11-01 (Sunday) is the fall-back date. Fri 10-30 is EDT, Mon 11-02 is EST."""
    assert match_slot(datetime(2026, 10, 30, 19, 55, tzinfo=UTC), SLOTS, WINDOW).name == "close"  # type: ignore[union-attr]  # 15:55 EDT
    assert (
        match_slot(datetime(2026, 10, 30, 20, 55, tzinfo=UTC), SLOTS, WINDOW) is None
    )  # 16:55 EDT
    assert match_slot(datetime(2026, 11, 2, 19, 55, tzinfo=UTC), SLOTS, WINDOW) is None  # 14:55 EST
    assert match_slot(datetime(2026, 11, 2, 20, 55, tzinfo=UTC), SLOTS, WINDOW).name == "close"  # type: ignore[union-attr]  # 15:55 EST
    assert not is_trading_day(date(2026, 11, 1))


def test_slot_utc_offsets_differ_across_dst() -> None:
    assert slot_time(OPEN, date(2026, 3, 6)).at_et.astimezone(UTC).hour == 14  # type: ignore[union-attr]
    assert slot_time(OPEN, date(2026, 3, 9)).at_et.astimezone(UTC).hour == 13  # type: ignore[union-attr]


def test_window_edges() -> None:
    st = slot_time(OPEN, date(2026, 11, 3))
    assert st is not None
    assert match_slot(st.at_et - timedelta(minutes=1), SLOTS, WINDOW) is None
    assert match_slot(st.at_et, SLOTS, WINDOW) is not None
    assert match_slot(st.at_et + timedelta(minutes=WINDOW), SLOTS, WINDOW) is not None
    assert match_slot(st.at_et + timedelta(minutes=WINDOW + 1), SLOTS, WINDOW) is None


def test_expected_slot_times_skip_weekend_holiday_and_use_half_day_time() -> None:
    start = datetime(2026, 11, 24, 0, 0, tzinfo=UTC)
    end = datetime(2026, 11, 30, 23, 0, tzinfo=UTC)
    got = expected_slot_times(SLOTS, start, end)
    days = sorted({s.date for s in got})
    assert date(2026, 11, 26) not in days  # Thanksgiving
    assert date(2026, 11, 28) not in days and date(2026, 11, 29) not in days
    assert [s.hhmm for s in got if s.date == date(2026, 11, 27)] == ["0945", "1250"]
    assert [s.hhmm for s in got if s.date == date(2026, 11, 25)] == ["0945", "1550"]
