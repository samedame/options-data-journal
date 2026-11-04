"""Derived positions (entry fill + its exits) and the contracts that must be pinned in snapshots.

Positions are never stored: they are recomputed from journal/fills and journal/exits.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from odj.common.config import journal_cfg
from odj.journal import store
from odj.journal.slippage import multiplier_for_legs, realized_pnl_usd
from odj.snapshot.enrich import PinKey, pin_key


@dataclass
class Position:
    fill: dict[str, Any]
    alert: dict[str, Any] | None
    exits: list[dict[str, Any]] = field(default_factory=list)
    multiplier: int = 100

    @property
    def contracts_closed(self) -> int:
        return sum(int(e["contracts"]) for e in self.exits)

    @property
    def contracts_open(self) -> int:
        return int(self.fill["contracts"]) - self.contracts_closed

    @property
    def is_closed(self) -> bool:
        return self.contracts_open <= 0

    def realized_pnl_usd(self) -> float:
        """Realized P&L in dollars over the contracts closed so far, after fees."""
        total = 0.0
        n_fill = int(self.fill["contracts"])
        for e in self.exits:
            alloc = float(self.fill.get("fees_total", 0.0)) * int(e["contracts"]) / n_fill
            total += realized_pnl_usd(
                float(self.fill["net_price"]),
                float(e["net_price"]),
                int(e["contracts"]),
                multiplier=self.multiplier,
                entry_fees_alloc=alloc,
                exit_fees=float(e.get("fees_total", 0.0)),
            )
        return total

    def realized_pnl_per_contract(self) -> float | None:
        n = self.contracts_closed
        return self.realized_pnl_usd() / n if n else None

    def earliest_expiry(self) -> str | None:
        exps = [leg["expiry"] for leg in self.fill.get("legs_actual", []) if leg.get("expiry")]
        return min(exps) if exps else None


def derive_positions(
    alerts: list[dict[str, Any]],
    fills: list[dict[str, Any]],
    exits: list[dict[str, Any]],
    multipliers: dict[str, int] | None = None,
) -> list[Position]:
    by_alert = {a["id"]: a for a in alerts}
    exits_by_fill: dict[str, list[dict[str, Any]]] = {}
    for e in exits:
        exits_by_fill.setdefault(e["fill_id"], []).append(e)
    out: list[Position] = []
    for f in fills:
        out.append(
            Position(
                fill=f,
                alert=by_alert.get(f.get("alert_id") or ""),
                exits=sorted(exits_by_fill.get(f["id"], []), key=lambda e: e["ts_utc"]),
                multiplier=multiplier_for_legs(f["legs_actual"], multipliers),
            )
        )
    return out


def load_positions(root: Path) -> list[Position]:
    cfg = journal_cfg(root)
    return derive_positions(
        store.read_all(root, "alert"),
        store.read_all(root, "fill"),
        store.read_all(root, "exit"),
        cfg.get("contract_multiplier"),
    )


def pinned_contracts(root: Path, today: date) -> dict[str, set[PinKey]]:
    """Exact (expiry, strike, right) of every alert/fill leg that has not expired yet, per underlying.

    These are always captured by the snapshotter, bypassing the DTE and moneyness filters, because
    the shadow tracker needs to mark them on every later snapshot until they expire.
    """
    out: dict[str, set[PinKey]] = {}
    iso = today.isoformat()
    for a in store.read_all(root, "alert"):
        for leg in a.get("legs", []):
            if leg.get("type") in ("call", "put") and leg.get("expiry", "") >= iso:
                out.setdefault(a["underlying"], set()).add(
                    pin_key(leg["expiry"], leg["strike"], leg["type"][0])
                )
    alerts_by_id = {a["id"]: a for a in store.read_all(root, "alert")}
    for f in store.read_all(root, "fill"):
        und = (alerts_by_id.get(f.get("alert_id") or "") or {}).get("underlying") or f.get(
            "meta", {}
        ).get("underlying")
        if not und:
            continue
        for leg in f.get("legs_actual", []):
            if leg.get("type") in ("call", "put") and (leg.get("expiry") or "") >= iso:
                out.setdefault(und, set()).add(
                    pin_key(leg["expiry"], leg["strike"], leg["type"][0])
                )
    return out
