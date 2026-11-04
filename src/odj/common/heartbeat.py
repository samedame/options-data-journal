"""status/heartbeat.json: per-job last-success bookkeeping used by the watchdog."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from odj.common.io import file_lock, read_json, write_json
from odj.common.timeutil import fmt_ts, now_utc, parse_ts

KEEP_DAYS = 21


def path(root: Path) -> Path:
    return root / "status" / "heartbeat.json"


def load(root: Path) -> dict[str, Any]:
    data = read_json(path(root), default=None)
    if not isinstance(data, dict) or "jobs" not in data:
        return {"schema_version": 1, "jobs": {}}
    return data


def record(
    root: Path,
    job: str,
    *,
    status: str = "ok",
    slot_key: str | None = None,
    detail: str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Record a job run. `slot_key` e.g. '2026-11-03/open' marks that specific slot done/failed."""
    when = now or now_utc()
    ts = fmt_ts(when)
    p = path(root)
    with file_lock(p):
        data = load(root)
        j = data["jobs"].setdefault(job, {"slots": {}})
        j["last_run_utc"] = ts
        j["last_status"] = status
        if detail:
            j["last_detail"] = detail[:300]
        if status == "ok":
            j["last_success_utc"] = ts
            if slot_key:
                j["last_slot"] = slot_key
        else:
            j["last_failure_utc"] = ts
        if slot_key:
            j.setdefault("slots", {})[slot_key] = {"status": status, "ts_utc": ts}
        _trim(j, when)
        write_json(p, data)
    return data


def _trim(job: dict[str, Any], now: datetime) -> None:
    cutoff = now - timedelta(days=KEEP_DAYS)
    slots = job.get("slots", {})
    for k in list(slots):
        try:
            if parse_ts(slots[k]["ts_utc"]) < cutoff:
                del slots[k]
        except (KeyError, ValueError):
            del slots[k]


def merge(
    base: dict[str, Any] | None, upstream: dict[str, Any], mine: dict[str, Any]
) -> dict[str, Any]:
    """Three-way semantic merge of two heartbeat documents (used when two runs race on git).

    For each job: if THIS run changed it relative to `base`, our fields win and slot maps are unioned
    with upstream's; otherwise upstream's entry is kept. Jobs written only by the other run survive."""
    jobs: dict[str, Any] = {}
    b_jobs = (base or {}).get("jobs") or {}
    u_jobs = upstream.get("jobs") or {}
    m_jobs = mine.get("jobs") or {}
    for name in sorted(set(u_jobs) | set(m_jobs)):
        u, m, b = u_jobs.get(name), m_jobs.get(name), b_jobs.get(name)
        if m is not None and m != b:
            merged = dict(u or {})
            merged.update({k: v for k, v in m.items() if k != "slots"})
            merged["slots"] = {**((u or {}).get("slots") or {}), **(m.get("slots") or {})}
            jobs[name] = merged
        else:
            jobs[name] = u if u is not None else m
    return {**upstream, "jobs": jobs}
