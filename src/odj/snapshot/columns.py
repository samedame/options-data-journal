"""The canonical chain-snapshot schema (column order and meaning)."""

from __future__ import annotations

FLAG_COLUMNS: list[str] = [
    "zero_bid",
    "zero_ask",
    "crossed_market",
    "wide_spread",
    "stale_last",
    "iv_divergence",
    "parity_violation",
    "missing_oi",
]

CHAIN_COLUMNS: list[str] = [
    "snapshot_ts_utc",
    "slot",
    "underlying",
    "underlying_price",
    "expiry",
    "dte",
    "strike",
    "right",
    "bid",
    "ask",
    "mid",
    "last",
    "last_trade_ts",
    "volume",
    "open_interest",
    "iv_vendor",
    "in_the_money",
    "iv_calc",
    "delta",
    "gamma",
    "theta",
    "vega",
    "r_used",
    "q_used",
    *FLAG_COLUMNS,
    "usable",
    "pinned",
]

# Provider output contract (what a Provider must supply per option row).
RAW_OPTION_COLUMNS: list[str] = [
    "expiry",
    "strike",
    "right",
    "bid",
    "ask",
    "last",
    "last_trade_ts",
    "volume",
    "open_interest",
    "iv_vendor",
    "in_the_money",
]

MARKET_KEYS = ["vix", "vix9d", "vix3m", "vxn", "rate"]
