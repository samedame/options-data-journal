"""`odj backfill`: optional historical-chain pull via Alpha Vantage. Never fails CI.

Backfilled days are stored with slot='eod' (file `1600.parquet`) and provider='alphavantage';
they are end-of-day data and must not be mixed with the 09:45/15:50 snapshots without care.
The underlying price is not in the AV payload, so it is inferred from put-call parity at the
at-the-money strike (flagged in meta as `underlying_price_inferred`).
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from odj.common.calendars import trading_days
from odj.common.config import find_root, tickers_cfg
from odj.common.io import atomic_write_bytes, dumps
from odj.common.timeutil import ET, fmt_ts
from odj.snapshot.compact import dir_size_bytes  # noqa: F401  (re-export convenience)
from odj.snapshot.enrich import build_chain
from odj.snapshot.providers.alphavantage import AlphaVantageProvider, AlphaVantageUnavailable
from odj.snapshot.providers.base import RawChain
from odj.snapshot.store import daily_path, meta_path, snapshot_exists, write_parquet

log = logging.getLogger("odj.backfill")


def infer_spot(opts: pd.DataFrame) -> float | None:
    """Spot ~ strike + (C_mid - P_mid) at the strike where |C_mid - P_mid| is smallest (parity, r~q~0)."""
    o = opts.assign(mid=(opts["bid"] + opts["ask"]) / 2.0)
    o = o[(o["bid"] > 0) & (o["ask"] > 0)]
    if o.empty:
        return None
    near = o.sort_values("expiry").groupby("expiry").head(0)  # placeholder to keep API stable
    del near
    first_exp = o["expiry"].min()
    e = o[o["expiry"] == first_exp]
    c = e[e["right"] == "C"].set_index("strike")["mid"]
    p = e[e["right"] == "P"].set_index("strike")["mid"]
    both = c.index.intersection(p.index)
    if len(both) == 0:
        return None
    diff = (c.loc[both] - p.loc[both]).abs()
    k = float(diff.idxmin())
    return k + float(c.loc[k] - p.loc[k])


def run_backfill(
    start: date,
    end: date,
    underlyings: list[str] | None = None,
    *,
    root: Path | None = None,
    provider: AlphaVantageProvider | None = None,
    force: bool = False,
) -> dict[str, Any]:
    root = root or find_root()
    cfg = tickers_cfg(root)
    prov = provider or AlphaVantageProvider(
        base_url=cfg["alphavantage"]["base_url"],
        timeout=int(cfg["alphavantage"]["timeout_seconds"]),
    )
    if not prov.configured:
        return {
            "status": "unavailable",
            "message": "ALPHAVANTAGE_API_KEY is not set; backfill skipped (exit 0).",
        }
    unds = underlyings or list(cfg["underlyings"]["core"])
    gd = cfg["greeks_defaults"]
    stored = 0
    skipped = 0
    for u in unds:
        for d in trading_days(start, end):
            if snapshot_exists(root, u, d, "eod", "1600") and not force:
                skipped += 1
                continue
            try:
                _, opts = prov.fetch_day(u, d)
            except AlphaVantageUnavailable as exc:
                return {
                    "status": "unavailable",
                    "message": f"Alpha Vantage historical options unavailable: {exc}",
                    "stored": stored,
                }
            spot = infer_spot(opts)
            if spot is None or not np.isfinite(spot):
                log.warning("%s %s: could not infer spot; skipping", u, d)
                continue
            ts = datetime(d.year, d.month, d.day, 16, 0, tzinfo=ET).astimezone(UTC)
            raw = RawChain(u, float(spot), ts, opts, "alphavantage")
            df = build_chain(
                raw,
                slot="eod",
                rate=float(gd["risk_free_rate"]),
                div_yield=float(gd["dividend_yield"].get(u, gd["dividend_yield"]["default"])),
                filters=cfg["snapshot"]["filters"],
                quality_cfg=cfg["quality"],
            )
            path = daily_path(root, u, d, "1600")
            size = write_parquet(
                df, path, cfg["storage"]["compression"], int(cfg["storage"]["compression_level"])
            )
            meta = {
                "underlying": u,
                "date": d.isoformat(),
                "slot": "eod",
                "hhmm": "1600",
                "snapshot_ts_utc": fmt_ts(ts),
                "underlying_price": float(spot),
                "underlying_price_inferred": True,
                "provider": "alphavantage",
                "rows": len(df),
                "greeks_inputs_defaulted": True,
                "parquet_bytes": size,
            }
            atomic_write_bytes(meta_path(path), dumps(meta).encode())
            stored += 1
    return {"status": "ok", "stored": stored, "skipped_existing": skipped}
