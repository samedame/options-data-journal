"""Time helpers. All persisted timestamps are UTC ISO-8601 with a trailing Z."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
TS_FMT = "%Y-%m-%dT%H:%M:%SZ"


def now_utc() -> datetime:
    return datetime.now(UTC)


def to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("naive datetime not allowed")
    return dt.astimezone(UTC)


def fmt_ts(dt: datetime) -> str:
    return to_utc(dt).strftime(TS_FMT)


def parse_ts(value: str | datetime) -> datetime:
    """Parse an ISO-8601 string (Z or offset). Naive strings are rejected."""
    if isinstance(value, datetime):
        return to_utc(value)
    s = value.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        raise ValueError(f"timestamp without timezone: {value!r}")
    return dt.astimezone(UTC)


def normalize_ts(value: str | datetime) -> str:
    """Normalize to 'YYYY-MM-DDTHH:MM:SSZ' (seconds precision, UTC)."""
    return fmt_ts(parse_ts(value))


def et_date(dt: datetime) -> date:
    return dt.astimezone(ET).date()


def et_datetime(d: date, hhmm: str) -> datetime:
    """The aware ET datetime for calendar date `d` at 'HH:MM' (DST-correct)."""
    h, m = (int(x) for x in hhmm.split(":"))
    return datetime.combine(d, time(h, m), tzinfo=ET)


def compact_ts(dt: datetime) -> str:
    return to_utc(dt).strftime("%Y%m%dT%H%M%SZ")


def month_key(ts: str | datetime) -> str:
    return parse_ts(ts).strftime("%Y-%m")


def minutes_between(a: datetime, b: datetime) -> float:
    return (b - a) / timedelta(minutes=1)
