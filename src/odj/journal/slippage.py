"""Slippage and P&L math. Pure functions.

Sign convention everywhere: per-share prices, CREDIT POSITIVE / DEBIT NEGATIVE, for the entry and
for each exit (an exit's net_price is the cash flow of the closing transaction: closing a credit
spread is a debit, so it is negative; an option expiring worthless is 0).

* entry slippage  = fill_net - alert_net        (negative = worse than the alert)
* exit slippage   = actual_exit_net - theoretical_exit_net   (negative = worse)
* P&L per share   = entry_net + exit_net
"""

from __future__ import annotations

from typing import Any


def multiplier_for_legs(
    legs: list[dict[str, Any]], multipliers: dict[str, int] | None = None
) -> int:
    m = multipliers or {"option": 100, "equity": 1}
    if legs and all(leg.get("type") == "stock" for leg in legs):
        return int(m["equity"])
    return int(m["option"])


def entry_slippage(
    alert_net: float,
    fill_net: float,
    *,
    multiplier: int = 100,
    max_loss_usd: float | None = None,
) -> dict[str, float | None]:
    per_share = fill_net - alert_net
    usd = per_share * multiplier
    pct_premium = (per_share / abs(alert_net) * 100.0) if alert_net else None
    pct_max_loss = (usd / max_loss_usd * 100.0) if max_loss_usd else None
    return {
        "per_share": round(per_share, 6),
        "usd_per_contract": round(usd, 4),
        "pct_of_alert_premium": None if pct_premium is None else round(pct_premium, 4),
        "pct_of_max_loss": None if pct_max_loss is None else round(pct_max_loss, 4),
    }


def exit_slippage(
    actual_net: float,
    theoretical_net: float,
    *,
    multiplier: int = 100,
    max_loss_usd: float | None = None,
    alert_net: float | None = None,
) -> dict[str, float | None]:
    per_share = actual_net - theoretical_net
    usd = per_share * multiplier
    pct_premium = (per_share / abs(alert_net) * 100.0) if alert_net else None
    pct_max_loss = (usd / max_loss_usd * 100.0) if max_loss_usd else None
    return {
        "per_share": round(per_share, 6),
        "usd_per_contract": round(usd, 4),
        "pct_of_alert_premium": None if pct_premium is None else round(pct_premium, 4),
        "pct_of_max_loss": None if pct_max_loss is None else round(pct_max_loss, 4),
    }


def realized_pnl_usd(
    entry_net: float,
    exit_net: float,
    contracts: int,
    *,
    multiplier: int = 100,
    entry_fees_alloc: float = 0.0,
    exit_fees: float = 0.0,
) -> float:
    """Dollar P&L for `contracts` closed, after fees."""
    return (entry_net + exit_net) * multiplier * contracts - entry_fees_alloc - exit_fees
