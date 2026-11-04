"""Quality flags. Rows are NEVER dropped here; they are flagged and `usable` is derived."""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from odj.snapshot.columns import FLAG_COLUMNS

DEFAULT_QUALITY: dict[str, Any] = {
    "wide_spread_pct": 0.50,
    "wide_spread_min_abs": 0.10,
    "stale_last_minutes": 60,
    "iv_divergence_abs": 0.10,
    "parity_spread_mult": 1.0,
    "parity_abs_floor": 0.10,
    "unusable_flags": ["zero_bid", "zero_ask", "crossed_market", "wide_spread"],
}


def parity_residual_flags(df: pd.DataFrame, q_cfg: dict[str, Any]) -> pd.Series:
    """Put-call parity check per (expiry, strike) pair.

    residual = (C_mid - P_mid) - (S*exp(-qT) - K*exp(-rT)).  A pair is a violation when
    |residual| > parity_spread_mult * (call_spread + put_spread) + parity_abs_floor, i.e. the
    parity gap cannot be explained by the quoted bid/ask widths. Needs `_T`, `r_used`, `q_used`.
    Both rows of a violating pair are flagged. Pairs lacking a two-sided quote on either side
    are skipped (not flagged).
    """
    out = pd.Series(False, index=df.index)
    ok = (df["bid"] > 0) & (df["ask"] > 0) & (df["ask"] >= df["bid"]) & df["mid"].notna()
    cols = [
        "expiry",
        "strike",
        "right",
        "mid",
        "bid",
        "ask",
        "underlying_price",
        "_T",
        "r_used",
        "q_used",
    ]
    work = df.loc[ok, cols]
    calls = work[work["right"] == "C"].rename(
        columns={"mid": "c_mid", "bid": "c_bid", "ask": "c_ask"}
    )
    puts = work[work["right"] == "P"].rename(
        columns={"mid": "p_mid", "bid": "p_bid", "ask": "p_ask"}
    )
    if calls.empty or puts.empty:
        return out
    m = calls.assign(idx_c=calls.index).merge(
        puts[["expiry", "strike", "p_mid", "p_bid", "p_ask"]].assign(idx_p=puts.index),
        on=["expiry", "strike"],
    )
    if m.empty:
        return out
    S, K, T = m["underlying_price"], m["strike"], m["_T"]
    forward_term = S * np.exp(-m["q_used"] * T) - K * np.exp(-m["r_used"] * T)
    resid = (m["c_mid"] - m["p_mid"]) - forward_term
    tol = float(q_cfg["parity_spread_mult"]) * (
        (m["c_ask"] - m["c_bid"]) + (m["p_ask"] - m["p_bid"])
    ) + float(q_cfg["parity_abs_floor"])
    bad = m[resid.abs() > tol]
    out.loc[bad["idx_c"].to_numpy()] = True
    out.loc[bad["idx_p"].to_numpy()] = True
    return out


def apply_flags(df: pd.DataFrame, q_cfg: dict[str, Any], snapshot_ts: pd.Timestamp) -> pd.DataFrame:
    """Add the eight boolean flag columns and `usable`. Requires: bid, ask, mid, last_trade_ts,
    iv_vendor, iv_calc, open_interest, underlying_price, strike, right, expiry, _T, r_used, q_used."""
    cfg = {**DEFAULT_QUALITY, **(q_cfg or {})}
    d = df.copy()
    bid, ask, mid = d["bid"], d["ask"], d["mid"]
    d["zero_bid"] = (bid.fillna(0) <= 0).to_numpy()
    d["zero_ask"] = (ask.fillna(0) <= 0).to_numpy()
    d["crossed_market"] = ((bid > ask) & (ask > 0)).fillna(False).to_numpy()
    spread = ask - bid
    d["wide_spread"] = (
        (
            ((spread / mid) > float(cfg["wide_spread_pct"]))
            & (spread > float(cfg["wide_spread_min_abs"]))
        )
        .fillna(False)
        .to_numpy()
    )
    d["last_trade_ts"] = pd.to_datetime(d["last_trade_ts"], utc=True)
    age_min = (snapshot_ts - d["last_trade_ts"]).dt.total_seconds() / 60.0
    d["stale_last"] = (
        d["last_trade_ts"].isna() | (age_min > float(cfg["stale_last_minutes"]))
    ).to_numpy()
    div = (d["iv_vendor"] - d["iv_calc"]).abs()
    d["iv_divergence"] = (div > float(cfg["iv_divergence_abs"])).fillna(False).to_numpy()
    d["parity_violation"] = parity_residual_flags(d, cfg).to_numpy()
    d["missing_oi"] = d["open_interest"].isna().to_numpy()
    unusable = [f for f in cfg["unusable_flags"] if f in FLAG_COLUMNS]
    d["usable"] = ~d[unusable].any(axis=1)
    return d


def summarize(df: pd.DataFrame) -> dict[str, Any]:
    """Row counts, per-flag counts, % usable (for the sidecar meta and the heartbeat)."""
    n = len(df)
    flags = {f: int(df[f].sum()) for f in FLAG_COLUMNS if f in df}
    usable = int(df["usable"].sum()) if "usable" in df else 0
    return {
        "rows": n,
        "usable_rows": usable,
        "usable_pct": round(100.0 * usable / n, 2) if n else 0.0,
        "flag_counts": flags,
    }
