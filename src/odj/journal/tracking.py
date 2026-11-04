"""I/O glue: build shadow results and per-alert "trade rows" from the journal + stored snapshots."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from odj.common.config import journal_cfg
from odj.common.timeutil import ET, fmt_ts, parse_ts
from odj.journal import store
from odj.journal.positions import derive_positions
from odj.journal.shadow import (
    ContractKey,
    MarkMode,
    Quote,
    ShadowResult,
    SnapView,
    contract_key,
    track_alert,
)
from odj.journal.slippage import entry_slippage, exit_slippage, multiplier_for_legs
from odj.snapshot.store import load_quotes, snapshot_index

Row = dict[str, Any]


def alert_keys(alert: dict[str, Any]) -> set[ContractKey]:
    return {
        contract_key(leg["expiry"], leg["strike"], leg["type"])
        for leg in alert.get("legs", [])
        if leg.get("type") in ("call", "put")
    }


def snapshot_views(
    root: Path, underlying: str, keys: set[ContractKey], start: date
) -> list[SnapView]:
    views: dict[str, SnapView] = {}
    for ent in snapshot_index(root, underlying):
        if ent.date < start:
            continue
        ts_s = ent.meta.get("snapshot_ts_utc")
        spot = ent.meta.get("underlying_price")
        if not ts_s or spot is None:
            continue
        views[ts_s] = SnapView(ts=parse_ts(ts_s), slot=ent.slot, underlying_price=float(spot))
    qdf = load_quotes(root, underlying, keys, start, date(2100, 1, 1))
    for ts, exp, strike, right, bid, ask in zip(
        qdf["snapshot_ts_utc"].tolist(),
        qdf["expiry"].tolist(),
        qdf["strike"].tolist(),
        qdf["right"].tolist(),
        qdf["bid"].tolist(),
        qdf["ask"].tolist(),
        strict=True,
    ):
        v = views.get(fmt_ts(ts.to_pydatetime()))
        if v is None:
            continue
        v.quotes[contract_key(exp.strftime("%Y-%m-%d"), float(strike), str(right))] = Quote(
            float(bid) if bid == bid else float("nan"),
            float(ask) if ask == ask else float("nan"),
        )
    return sorted(views.values(), key=lambda v: v.ts)


def track_alerts(
    root: Path,
    alerts: list[dict[str, Any]],
    mark_mode: MarkMode = "mid",
) -> dict[str, ShadowResult]:
    cfg = journal_cfg(root)
    slot = str(cfg.get("shadow", {}).get("settlement_snapshot_slot", "close"))
    by_und: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for a in alerts:
        by_und[a["underlying"]].append(a)
    out: dict[str, ShadowResult] = {}
    for und, alist in by_und.items():
        keys: set[ContractKey] = set()
        for a in alist:
            keys |= alert_keys(a)
        start = min(parse_ts(a["ts_utc"]).astimezone(ET).date() for a in alist)
        views = snapshot_views(root, und, keys, start)
        for a in alist:
            out[a["id"]] = track_alert(a, views, mark_mode, settlement_slot=slot)
    return out


def build_rows(
    root: Path,
    *,
    now: datetime,
    window_days: int | None = None,
    mark_mode: MarkMode | None = None,
) -> tuple[list[Row], dict[str, ShadowResult]]:
    cfg = journal_cfg(root)
    mode: MarkMode = mark_mode or cfg.get("default_mark_mode", "mid")
    all_alerts = store.read_all(root, "alert")
    alerts = all_alerts
    if window_days is not None:
        cutoff = now - timedelta(days=window_days)
        alerts = [a for a in all_alerts if parse_ts(a["ts_utc"]) >= cutoff]
    fills = store.read_all(root, "fill")
    exits = store.read_all(root, "exit")
    skips = store.read_all(root, "skip")
    positions = derive_positions(all_alerts, fills, exits, cfg.get("contract_multiplier"))
    shadow = track_alerts(root, alerts, mode)

    pos_by_alert: dict[str, list[Any]] = defaultdict(list)
    for p in positions:
        if p.fill.get("alert_id"):
            pos_by_alert[p.fill["alert_id"]].append(p)
    skip_by_alert = {s["alert_id"]: s for s in skips}

    rows: list[Row] = []
    for a in alerts:
        sh = shadow[a["id"]]
        pos = sorted(pos_by_alert.get(a["id"], []), key=lambda p: p.fill["ts_utc"])
        row: Row = {
            "alert_id": a["id"],
            "ts_utc": a["ts_utc"],
            "bot": a["bot"],
            "strategy": a["strategy"],
            "structure": a["structure"],
            "underlying": a["underlying"],
            "confidence_score": a.get("confidence_score"),
            "regime_gate_state": a.get("regime_gate_state"),
            "decision": "none",
            "skip_reason": None,
            "entry_slip": None,
            "latency_s": None,
            "exit_slip": None,
            "round_trip_slip_usd": None,
            "realized_pnl_usd": None,
            "contracts_closed": None,
            "shadow_status": sh.status,
            "shadow_reason": sh.reason,
            "shadow_exit_reason": sh.exit_reason,
            "theoretical_pnl": sh.theoretical_pnl,
            "theoretical_exit_price": sh.theoretical_exit_price,
        }
        if pos:
            row["decision"] = "filled"
            total_c = sum(int(p.fill["contracts"]) for p in pos)
            fill_net = (
                sum(float(p.fill["net_price"]) * int(p.fill["contracts"]) for p in pos) / total_c
            )
            mult = multiplier_for_legs(pos[0].fill["legs_actual"], cfg.get("contract_multiplier"))
            row["entry_slip"] = entry_slippage(
                float(a["alert_net_price"]),
                fill_net,
                multiplier=mult,
                max_loss_usd=a.get("max_loss"),
            )
            first = pos[0].fill
            if first.get("ts_precision", "second") == "second":
                row["latency_s"] = max(
                    0.0, (parse_ts(first["ts_utc"]) - parse_ts(a["ts_utc"])).total_seconds()
                )
            if all(p.is_closed for p in pos):
                closed_c = sum(p.contracts_closed for p in pos)
                row["contracts_closed"] = closed_c
                row["realized_pnl_usd"] = sum(p.realized_pnl_usd() for p in pos)
                if sh.status == "closed" and sh.theoretical_exit_price is not None and closed_c:
                    exit_net = (
                        sum(
                            float(e["net_price"]) * int(e["contracts"])
                            for p in pos
                            for e in p.exits
                        )
                        / closed_c
                    )
                    row["exit_slip"] = exit_slippage(
                        exit_net,
                        sh.theoretical_exit_price,
                        multiplier=mult,
                        max_loss_usd=a.get("max_loss"),
                        alert_net=float(a["alert_net_price"]),
                    )
                    row["round_trip_slip_usd"] = -(
                        float(row["entry_slip"]["usd_per_contract"] or 0.0)
                        + float(row["exit_slip"]["usd_per_contract"] or 0.0)
                    )
        elif a["id"] in skip_by_alert:
            row["decision"] = "skipped"
            row["skip_reason"] = skip_by_alert[a["id"]]["reason"]
        rows.append(row)
    return rows, shadow
