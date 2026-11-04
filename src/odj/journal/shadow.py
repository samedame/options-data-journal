"""Shadow tracker: mechanically replay an alert's exit rules on stored snapshots.

IMPORTANT LIMITS (repeated in every report footer): marks are taken at SNAPSHOT granularity
(twice a day) from delayed, indicative Yahoo data. A profit target or stop touched only
intraday between snapshots is MISSED. Expiry settlement uses the underlying price at the
close-slot snapshot of expiry day (~15:50 ET) as a proxy for the official settlement price.
This is an approximation, not a tick-accurate replay. No interpolation is ever performed.

All functions here are pure (no I/O).

Rule semantics (documented in schemas/alert.schema.json):
* profit_target_pct_of_credit = p : exit when unrealized P&L >= p% of |alert_net_price|
* stop_loss_multiple_of_credit = m : exit when unrealized P&L <= -m * |alert_net_price|
* dte_exit = d : exit at the first snapshot where (earliest expiry - snapshot ET date) <= d days
* time_exit : ISO datetime string -> first snapshot at/after it; integer n -> n calendar days after the alert
* Priority at one snapshot: stop, profit_target, dte, time. Earlier snapshots always win.
* If nothing triggers, the position is settled at expiry (intrinsic values at the close-slot
  underlying price of expiry day).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal

from odj.common.timeutil import ET, fmt_ts, parse_ts

ContractKey = tuple[str, float, str]  # (expiry 'YYYY-MM-DD', strike rounded to 4dp, 'C'|'P')
MarkMode = Literal["mid", "conservative"]
MULTIPLIER = 100


@dataclass(frozen=True)
class Quote:
    bid: float
    ask: float

    @property
    def valid(self) -> bool:
        return (
            not (math.isnan(self.bid) or math.isnan(self.ask))
            and self.ask > 0
            and self.bid >= 0
            and self.bid <= self.ask
        )

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0


@dataclass
class SnapView:
    ts: datetime  # aware UTC
    slot: str
    underlying_price: float
    quotes: dict[ContractKey, Quote] = field(default_factory=dict)

    @property
    def et_date(self) -> date:
        return self.ts.astimezone(ET).date()


@dataclass
class ShadowResult:
    alert_id: str
    status: Literal["closed", "open", "unmarkable"]
    mark_mode: str
    exit_reason: str | None = None
    theoretical_exit_ts: str | None = None
    theoretical_exit_price: float | None = None  # per share, close cash flow (credit +, debit -)
    theoretical_pnl: float | None = None  # dollars per contract, BEFORE fees
    unrealized_pnl: float | None = None  # last mark if still open
    n_marks: int = 0
    n_gap_snapshots: int = 0
    reason: str | None = None  # why unmarkable / why still open
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def contract_key(expiry: str, strike: float, right: str) -> ContractKey:
    return (expiry, round(float(strike), 4), right.upper()[:1])


def _leg_key(leg: dict[str, Any]) -> ContractKey:
    return contract_key(leg["expiry"], leg["strike"], leg["type"])


def close_cashflow(
    legs: list[dict[str, Any]], quotes: dict[ContractKey, Quote], mode: MarkMode
) -> float | None:
    """Per-share cash flow of closing all legs now (credit +, debit -). None if any quote is
    missing/invalid. `mid`: every leg at mid. `conservative`: sell legs we hold at the bid, buy back
    shorts at the ask (the adverse side)."""
    cash = 0.0
    for leg in legs:
        q = quotes.get(_leg_key(leg))
        if q is None or not q.valid:
            return None
        ratio = int(leg.get("ratio", 1))
        if leg["side"] == "buy":  # long leg: sell to close
            px = q.mid if mode == "mid" else q.bid
            cash += px * ratio
        else:  # short leg: buy to close
            px = q.mid if mode == "mid" else q.ask
            cash -= px * ratio
    return cash


def settlement_cashflow(legs: list[dict[str, Any]], spot: float) -> float:
    """Per-share cash flow at expiry: long legs receive intrinsic, short legs pay it."""
    cash = 0.0
    for leg in legs:
        k = float(leg["strike"])
        intrinsic = max(spot - k, 0.0) if leg["type"] == "call" else max(k - spot, 0.0)
        ratio = int(leg.get("ratio", 1))
        cash += intrinsic * ratio if leg["side"] == "buy" else -intrinsic * ratio
    return cash


def _unmarkable(alert_id: str, mode: str, reason: str) -> ShadowResult:
    return ShadowResult(alert_id=alert_id, status="unmarkable", mark_mode=mode, reason=reason)


def _expiry_close_utc(expiry: date) -> datetime:
    return datetime(expiry.year, expiry.month, expiry.day, 16, 0, tzinfo=ET).astimezone(UTC)


def track_alert(
    alert: dict[str, Any],
    snapshots: list[SnapView],
    mark_mode: MarkMode = "mid",
    *,
    settlement_slot: str = "close",
) -> ShadowResult:
    aid = str(alert["id"])
    legs = list(alert.get("legs", []))
    opt_legs = [leg for leg in legs if leg.get("type") in ("call", "put")]
    if not opt_legs or len(opt_legs) != len(legs):
        return _unmarkable(
            aid, mark_mode, "shadow tracker supports option-only structures (no stock legs)"
        )
    expiries = {leg["expiry"] for leg in opt_legs}
    if len(expiries) != 1:
        return _unmarkable(aid, mark_mode, "multi-expiry structures are not supported")
    entry = float(alert["alert_net_price"])
    basis = abs(entry)
    if basis == 0:
        return _unmarkable(aid, mark_mode, "alert_net_price is 0; percent rules are undefined")

    rules = alert.get("exit_rules") or {}
    expiry = date.fromisoformat(next(iter(expiries)))
    entry_ts = parse_ts(alert["ts_utc"])
    snaps = sorted((s for s in snapshots if s.ts > entry_ts), key=lambda s: s.ts)
    keys = {_leg_key(leg) for leg in opt_legs}
    present = {k for s in snaps for k in s.quotes if k in keys}
    missing = sorted(keys - present)
    if missing:
        names = ", ".join(f"{e} {k:g}{r}" for e, k, r in missing)
        return _unmarkable(aid, mark_mode, f"leg(s) never present in any later snapshot: {names}")

    max_loss_usd = alert.get("max_loss")
    res = ShadowResult(alert_id=aid, status="open", mark_mode=mark_mode)
    p_target = rules.get("profit_target_pct_of_credit")
    stop_mult = rules.get("stop_loss_multiple_of_credit")
    dte_exit = rules.get("dte_exit")
    time_exit = rules.get("time_exit")
    time_exit_ts: datetime | None = None
    if isinstance(time_exit, str):
        try:
            time_exit_ts = parse_ts(time_exit)
        except ValueError:
            res.notes.append(f"time_exit {time_exit!r} not parseable; ignored")
    elif isinstance(time_exit, int) and not isinstance(time_exit, bool):
        time_exit_ts = entry_ts + timedelta(days=time_exit)

    def close_out(s: SnapView, reason: str, cash: float) -> ShadowResult:
        res.status = "closed"
        res.exit_reason = reason
        res.theoretical_exit_ts = fmt_ts(s.ts)
        res.theoretical_exit_price = round(cash, 6)
        res.theoretical_pnl = round((entry + cash) * MULTIPLIER, 4)
        res.unrealized_pnl = None
        res.reason = None
        return res

    last_pnl: float | None = None
    for s in snaps:
        dte = (expiry - s.et_date).days
        cash = close_cashflow(opt_legs, s.quotes, mark_mode)
        on_settlement = s.et_date == expiry and (
            s.slot == settlement_slot or s.ts >= _expiry_close_utc(expiry)
        )
        if cash is None:
            res.n_gap_snapshots += 1
        else:
            res.n_marks += 1
            pnl = entry + cash
            last_pnl = pnl * MULTIPLIER
            if stop_mult is not None and pnl <= -float(stop_mult) * basis:
                return close_out(s, "stop", cash)
            if p_target is not None and pnl >= float(p_target) / 100.0 * basis:
                return close_out(s, "profit_target", cash)
            if dte_exit is not None and dte <= int(dte_exit):
                return close_out(s, "dte", cash)
            if time_exit_ts is not None and s.ts >= time_exit_ts:
                return close_out(s, "time", cash)
        if on_settlement:
            cash_s = settlement_cashflow(opt_legs, s.underlying_price)
            pnl_usd = (entry + cash_s) * MULTIPLIER
            if cash_s == 0:
                reason = "expiry_worthless"
            elif max_loss_usd is not None and pnl_usd <= -float(max_loss_usd) + 0.01:
                reason = "expiry_max_loss"
            else:
                reason = "expiry_settled"
            res.notes.append("settled using close-slot underlying price as proxy for settlement")
            return close_out(s, reason, cash_s)
        if s.et_date > expiry:
            break

    if snaps and snaps[-1].et_date >= expiry and res.status == "open":
        return _unmarkable(
            aid,
            mark_mode,
            "expiry reached but no close-slot snapshot on expiry date; cannot settle without interpolating",
        )
    res.unrealized_pnl = None if last_pnl is None else round(last_pnl, 4)
    res.reason = "still open: no rule triggered and expiry not reached"
    if res.n_gap_snapshots:
        res.notes.append(f"{res.n_gap_snapshots} snapshot(s) skipped for missing/invalid quotes")
    return res
