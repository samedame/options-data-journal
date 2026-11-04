"""Turn a provider's RawChain into the canonical, filtered, enriched snapshot DataFrame."""

from __future__ import annotations

from collections.abc import Collection
from typing import Any, cast

import numpy as np
import pandas as pd

from odj.common.timeutil import ET
from odj.snapshot.bs import bs_greeks, implied_vol
from odj.snapshot.columns import CHAIN_COLUMNS
from odj.snapshot.providers.base import RawChain
from odj.snapshot.quality import apply_flags

PinKey = tuple[str, float, str]  # (expiry 'YYYY-MM-DD', strike, 'C'|'P')
SECONDS_PER_YEAR = 365.0 * 24 * 3600


def pin_key(expiry: str, strike: float, right: str) -> PinKey:
    return (expiry, round(float(strike), 4), right.upper()[:1])


def expiry_close_utc(expiry: pd.Series) -> pd.Series:
    """16:00 America/New_York on the expiry date, as UTC timestamps (SPY/QQQ/IWM are PM-settled)."""
    local = (expiry.dt.normalize() + pd.Timedelta(hours=16)).dt.tz_localize(
        ET, ambiguous="NaT", nonexistent="shift_forward"
    )
    return cast(pd.Series, local.dt.tz_convert("UTC"))


def build_chain(
    raw: RawChain,
    *,
    slot: str,
    rate: float,
    div_yield: float,
    filters: dict[str, Any],
    pinned: Collection[PinKey] = (),
    quality_cfg: dict[str, Any] | None = None,
) -> pd.DataFrame:
    """Filter (DTE / moneyness, with pinned contracts bypassing both), then compute mid, IV, greeks,
    quality flags. Rows are never silently dropped by quality checks - only by the configured filters."""
    snap_ts = pd.Timestamp(raw.fetched_at).tz_convert("UTC")
    spot = float(raw.underlying_price)
    d = raw.options.copy()
    d["expiry"] = pd.to_datetime(d["expiry"]).astype("datetime64[ns]")
    d["strike"] = d["strike"].astype("float64")
    d["right"] = d["right"].astype(str).str.upper().str[0]
    for c in ("bid", "ask", "last", "volume", "open_interest", "iv_vendor"):
        d[c] = pd.to_numeric(d[c], errors="coerce").astype("float64")
    d["last_trade_ts"] = pd.to_datetime(d["last_trade_ts"], utc=True, errors="coerce")

    snap_et_date = snap_ts.tz_convert(ET).normalize().tz_localize(None)
    d["dte"] = (d["expiry"] - snap_et_date).dt.days.astype("int32")

    keys = pd.Series(
        [
            pin_key(e.strftime("%Y-%m-%d"), s, r)
            for e, s, r in zip(d["expiry"], d["strike"], d["right"], strict=True)
        ],
        index=d.index,
    )
    pin_set = set(pinned)
    d["pinned"] = keys.isin(pin_set).to_numpy() if pin_set else False

    in_dte = (d["dte"] >= int(filters.get("dte_min", 0))) & (
        d["dte"] <= int(filters.get("dte_max", 60))
    )
    pct = float(filters.get("moneyness_pct", 15)) / 100.0
    in_money = (d["strike"] / spot - 1.0).abs() <= pct
    d = d[(in_dte & in_money) | d["pinned"]].copy()

    d["snapshot_ts_utc"] = snap_ts
    d["slot"] = slot
    d["underlying"] = raw.underlying
    d["underlying_price"] = spot
    d["r_used"] = rate
    d["q_used"] = div_yield

    ask_ok = (d["ask"] > 0) & (d["ask"] >= d["bid"].fillna(0))
    d["mid"] = np.where(ask_ok, (d["bid"].fillna(0) + d["ask"]) / 2.0, np.nan)

    exp_close = expiry_close_utc(d["expiry"])
    d["_T"] = ((exp_close - snap_ts).dt.total_seconds() / SECONDS_PER_YEAR).clip(lower=0.0)
    is_call = (d["right"] == "C").to_numpy()
    S = np.full(len(d), spot)
    K = d["strike"].to_numpy()
    T = d["_T"].to_numpy()
    r = np.full(len(d), rate)
    q = np.full(len(d), div_yield)
    iv = implied_vol(d["mid"].to_numpy(), S, K, T, r, q, is_call)
    d["iv_calc"] = iv
    g = bs_greeks(S, K, T, r, q, np.where(np.isnan(iv), np.nan, iv), is_call)
    no_iv = np.isnan(iv)
    for name, arr in g.items():
        d[name] = np.where(
            no_iv, np.nan, arr
        )  # greeks are derived from iv_calc: no IV => no greeks
    d["in_the_money"] = np.where(d["right"] == "C", d["strike"] < spot, d["strike"] > spot)

    d = apply_flags(d, quality_cfg or {}, snap_ts)
    d = d.sort_values(["expiry", "right", "strike"], kind="stable").reset_index(drop=True)
    for c in ("iv_calc", "delta", "gamma", "theta", "vega", "iv_vendor", "r_used", "q_used"):
        d[c] = d[c].astype("float32")
    d["volume"] = d["volume"].astype("float64")
    return d[CHAIN_COLUMNS]
