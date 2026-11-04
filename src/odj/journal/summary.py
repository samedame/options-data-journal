"""data/summary/latest.json: the stable, versioned summary BotDeck reads (schemas/summary.schema.json)."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from odj.common import heartbeat
from odj.common.config import core_and_extra, find_root, journal_cfg, tickers_cfg
from odj.common.io import read_json, write_json
from odj.common.timeutil import fmt_ts, now_utc, parse_ts
from odj.journal import metrics, store
from odj.journal.positions import derive_positions
from odj.journal.tracking import build_rows
from odj.snapshot.store import chains_dir, snapshot_index

log = logging.getLogger("odj.summary")
SCHEMA_VERSION = 1
DISCLAIMER = (
    "Snapshot marks come from delayed, indicative Yahoo Finance data taken twice daily; "
    "shadow outcomes are approximations, not a tick-accurate replay."
)


def summary_path(root: Path) -> Path:
    return root / "data" / "summary" / "latest.json"


def _bots(root: Path, cfg: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    alerts = store.read_all(root, "alert")
    fills = store.read_all(root, "fill")
    exits = store.read_all(root, "exit")
    skips = store.read_all(root, "skip")
    by_id = {a["id"]: a for a in alerts}
    positions = derive_positions(alerts, fills, exits, cfg.get("contract_multiplier"))
    bots: dict[str, dict[str, Any]] = {}

    def slot(b: str) -> dict[str, Any]:
        return bots.setdefault(
            b,
            {
                "alerts": 0,
                "fills": 0,
                "skips": 0,
                "exits": 0,
                "open_positions": 0,
                "last_alert_ts_utc": None,
                "last_fill_ts_utc": None,
            },
        )

    for a in alerts:
        s = slot(a["bot"])
        s["alerts"] += 1
        s["last_alert_ts_utc"] = max(filter(None, [s["last_alert_ts_utc"], a["ts_utc"]]))
    fill_bot = {
        f["id"]: (by_id.get(f.get("alert_id") or "") or {}).get("bot", "unattributed")
        for f in fills
    }
    for f in fills:
        s = slot(fill_bot[f["id"]])
        s["fills"] += 1
        s["last_fill_ts_utc"] = max(filter(None, [s["last_fill_ts_utc"], f["ts_utc"]]))
    for e in exits:
        slot(fill_bot.get(e["fill_id"], "unattributed"))["exits"] += 1
    for sk in skips:
        slot((by_id.get(sk["alert_id"]) or {}).get("bot", "unattributed"))["skips"] += 1
    open_positions: list[dict[str, Any]] = []
    for p in positions:
        if p.is_closed:
            continue
        bot = fill_bot[p.fill["id"]]
        slot(bot)["open_positions"] += 1
        open_positions.append(
            {
                "fill_id": p.fill["id"],
                "alert_id": p.fill.get("alert_id"),
                "bot": bot,
                "underlying": (p.alert or {}).get("underlying"),
                "structure": (p.alert or {}).get("structure"),
                "contracts_open": p.contracts_open,
                "entry_ts_utc": p.fill["ts_utc"],
                "entry_net_price": p.fill["net_price"],
                "earliest_expiry": p.earliest_expiry(),
            }
        )
    return dict(sorted(bots.items())), open_positions


def _rolling(
    rows: list[dict[str, Any]], cfg: dict[str, Any], windows: list[int], now: datetime
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for w in windows:
        cutoff = now - timedelta(days=w)
        wr = [r for r in rows if parse_ts(r["ts_utc"]) >= cutoff]
        m = metrics.compute_all(wr, cfg)
        out[f"{w}d"] = {
            "window_days": w,
            "alerts": m["alerts"],
            "take_rate": m["take_rate"],
            "entry_slippage_usd_per_contract": {
                "n": m["entry_slippage"]["n"],
                "insufficient_sample": m["entry_slippage"]["insufficient_sample"],
                "mean": m["entry_slippage"]["mean_usd_per_contract"],
            },
            "entry_slippage_pct_of_credit": {
                "n": m["entry_slippage"]["n"],
                "insufficient_sample": m["entry_slippage"]["insufficient_sample"],
                "mean": m["entry_slippage"]["mean_pct_of_alert_premium"],
            },
            "latency_seconds": m["latency"],
            "capture_ratio": m["capture_ratio"],
            "slippage_vs_edge": m["slippage_vs_edge"],
            "unmarkable": m["unmarkable"]["count"],
            "error": None,
        }
    return out


def _data_collection(root: Path, cfg: dict[str, Any], hb: dict[str, Any]) -> dict[str, Any]:
    names = set(core_and_extra(cfg))
    cdir = chains_dir(root)
    if cdir.exists():
        names |= {p.name for p in cdir.iterdir() if p.is_dir()}
    snap_hb = (hb.get("jobs") or {}).get("snapshot") or {}
    out: dict[str, Any] = {}
    for u in sorted(names):
        idx = snapshot_index(root, u)
        last = idx[-1] if idx else None
        out[u] = {
            "days_collected": len({e.date for e in idx}),
            "snapshots": len(idx),
            "first_date": idx[0].date.isoformat() if idx else None,
            "last_date": last.date.isoformat() if last else None,
            "last_snapshot_ts_utc": (last.meta.get("snapshot_ts_utc") if last else None),
            "last_snapshot_status": snap_hb.get("last_status"),
            "last_usable_pct": (last.meta.get("usable_pct") if last else None),
        }
    return out


def build_summary(root: Path, now: datetime | None = None) -> dict[str, Any]:
    now = now or now_utc()
    jcfg = journal_cfg(root)
    tcfg = tickers_cfg(root)
    hb = heartbeat.load(root)
    bots, open_pos = _bots(root, jcfg)
    windows = [int(w) for w in jcfg["report"]["rolling_windows"]]
    try:
        rows, _ = build_rows(root, now=now, window_days=max(windows))
        rolling = _rolling(rows, jcfg, windows, now)
    except Exception as exc:  # shadow tracking must never prevent the summary from being written
        log.warning("rolling metrics failed: %s", exc)
        rolling = {
            f"{w}d": {"window_days": w, "error": f"{type(exc).__name__}: {exc}"} for w in windows
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at_utc": fmt_ts(now),
        "min_sample": int(jcfg["min_sample"]),
        "bots": bots,
        "open_positions": open_pos,
        "rolling": rolling,
        "data_collection": _data_collection(root, tcfg, hb),
        "heartbeat": {
            job: {
                "last_success_utc": j.get("last_success_utc"),
                "last_slot": j.get("last_slot"),
                "last_status": j.get("last_status"),
            }
            for job, j in sorted((hb.get("jobs") or {}).items())
        },
        "disclaimer": DISCLAIMER,
    }


def validate_summary(root: Path, doc: dict[str, Any]) -> list[str]:
    schema = read_json(root / "schemas" / "summary.schema.json")
    v = Draft202012Validator(schema)
    return [f"{'/'.join(str(p) for p in e.absolute_path)}: {e.message}" for e in v.iter_errors(doc)]


def write_summary(root: Path | None = None, *, now: datetime | None = None) -> dict[str, Any]:
    root = root or find_root()
    doc = build_summary(root, now)
    errs = validate_summary(root, doc)
    if errs:
        raise ValueError("summary does not match its own schema: " + "; ".join(errs[:5]))
    write_json(summary_path(root), doc)
    return doc
