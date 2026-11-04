"""Watchdog: did every scheduled job run in its expected window? Uses the generic staleness logic."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from odj.common import heartbeat
from odj.common.calendars import expected_slot_times
from odj.common.config import find_root, journal_cfg, tickers_cfg
from odj.common.staleness import overdue
from odj.common.timeutil import ET, now_utc, parse_ts


def _ok_times(job: dict[str, Any], slot_suffix: str | None = None) -> list[datetime]:
    out: list[datetime] = []
    for key, s in (job.get("slots") or {}).items():
        if s.get("status") != "ok":
            continue
        if slot_suffix is not None and not key.endswith("/" + slot_suffix):
            continue
        out.append(parse_ts(s["ts_utc"]))
    return out


def weekly_expected(now: datetime, cfg: dict[str, Any], lookback_days: int = 9) -> list[datetime]:
    w = cfg["report"]["weekly"]
    h, m = (int(x) for x in str(w["time"]).split(":"))
    out: list[datetime] = []
    d = now.astimezone(ET).date()
    for i in range(lookback_days + 1):
        day = d - timedelta(days=i)
        if day.strftime("%a").upper() == str(w["dow"]).upper()[:3]:
            t = datetime(day.year, day.month, day.day, h, m, tzinfo=ET)
            if t <= now:
                out.append(t)
    return out


def check(root: Path | None = None, *, now: datetime | None = None) -> list[str]:
    """Return human-readable problems (empty list = healthy)."""
    root = root or find_root()
    now = now or now_utc()
    tcfg, jcfg = tickers_cfg(root), journal_cfg(root)
    wd = jcfg["watchdog"]
    hb = heartbeat.load(root)
    jobs = hb.get("jobs") or {}
    problems: list[str] = []

    grace = timedelta(minutes=int(wd["grace_minutes"]))
    look = timedelta(hours=int(wd["lookback_hours"]))
    snap = jobs.get("snapshot") or {}
    for slot_cfg in tcfg["snapshot"]["slots"]:
        name = slot_cfg["name"]
        expected = [s.at_et for s in expected_slot_times([slot_cfg], now - look, now)]
        late = overdue(expected, _ok_times(snap, name), now, grace)
        for t in late:
            mins = int((now - t).total_seconds() // 60)
            problems.append(
                f"snapshot slot `{name}` expected {t:%Y-%m-%d %H:%M} ET has not completed ({mins} min ago)"
            )
    if snap.get("last_status") == "failed" and snap.get("last_failure_utc"):
        age = now - parse_ts(snap["last_failure_utc"])
        if age < timedelta(hours=12):
            problems.append(
                f"last snapshot run FAILED at {snap['last_failure_utc']}: {snap.get('last_detail', '')}"
            )

    wk = wd["jobs"].get("weekly_report")
    if wk:
        wgrace = timedelta(minutes=int(wk.get("grace_minutes", 180)))
        exp = weekly_expected(now, jcfg)
        late = (
            overdue(exp[:1], _ok_times(jobs.get("weekly_report") or {}), now, wgrace) if exp else []
        )
        for t in late:
            problems.append(f"weekly report expected {t:%Y-%m-%d %H:%M} ET has not run")
    return problems
