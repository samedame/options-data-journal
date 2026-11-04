"""Shared test builders. Everything here is SYNTHETIC."""

from __future__ import annotations

import copy
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from odj.common.timeutil import et_datetime
from odj.snapshot.columns import RAW_OPTION_COLUMNS
from odj.snapshot.providers.base import Provider, Quote, RawChain

FIXTURES = Path(__file__).parent / "fixtures"


def utc(d: date, hhmm: str) -> datetime:
    return et_datetime(d, hhmm).astimezone(UTC)


BASE_ALERT: dict[str, Any] = {
    "id": "spy-bps-bot-20261102T143500Z-1",
    "ts_utc": "2026-11-02T14:35:00Z",
    "bot": "spy-bps-bot",
    "strategy": "S1",
    "underlying": "SPY",
    "structure": "put_credit_spread",
    "legs": [
        {"side": "sell", "type": "put", "strike": 530.0, "expiry": "2026-11-20", "ratio": 1},
        {"side": "buy", "type": "put", "strike": 525.0, "expiry": "2026-11-20", "ratio": 1},
    ],
    "contracts_suggested": 2,
    "alert_net_price": 0.90,
    "alert_price_basis": "mid",
    "max_profit": 90.0,
    "max_loss": 410.0,
    "exit_rules": {"profit_target_pct_of_credit": 50, "stop_loss_multiple_of_credit": 2.0},
    "confidence_score": 70,
    "regime_gate_state": "on",
    "notes": "synthetic",
}


def alert(**over: Any) -> dict[str, Any]:
    a = copy.deepcopy(BASE_ALERT)
    a.update(over)
    return a


def fill(alert_id: str | None = BASE_ALERT["id"], **over: Any) -> dict[str, Any]:
    f: dict[str, Any] = {
        "id": "fill-test-1",
        "alert_id": alert_id,
        "ts_utc": "2026-11-02T14:40:00Z",
        "legs_actual": copy.deepcopy(BASE_ALERT["legs"]),
        "contracts": 2,
        "net_price": 0.85,
        "fees_total": 1.3,
        "order_type": "limit",
        "source": "cli",
    }
    f.update(over)
    return f


def exit_(fill_id: str = "fill-test-1", **over: Any) -> dict[str, Any]:
    e: dict[str, Any] = {
        "id": "exit-test-1",
        "fill_id": fill_id,
        "ts_utc": "2026-11-10T15:00:00Z",
        "contracts": 2,
        "net_price": -0.25,
        "fees_total": 1.3,
        "reason": "profit_target",
        "source": "cli",
    }
    e.update(over)
    return e


class FixtureProvider(Provider):
    """Replays a recorded (here: synthetic) chain from tests/fixtures/*.json."""

    name = "fixture"

    def __init__(self, path: Path, now: datetime):
        self.doc = json.loads(path.read_text())
        self.now = now

    def expirations(self, underlying: str) -> list[date]:
        return sorted({date.fromisoformat(o["expiry"]) for o in self.doc["options"]})

    def fetch_chain(self, underlying: str, expiries: list[date]) -> RawChain:
        keep = {e.isoformat() for e in expiries}
        rows = [o for o in self.doc["options"] if o["expiry"] in keep]
        df = pd.DataFrame(rows)
        df["expiry"] = pd.to_datetime(df["expiry"])
        df["last_trade_ts"] = pd.Timestamp(self.now) - pd.Timedelta(minutes=5)
        df["in_the_money"] = pd.array([False] * len(df), dtype="boolean")
        return RawChain(
            underlying,
            float(self.doc["underlying_price"]),
            self.now,
            df[RAW_OPTION_COLUMNS],
            self.name,
        )

    def fetch_quote(self, symbol: str) -> Quote | None:
        v = self.doc["market"].get(symbol)
        return None if v is None else Quote(float(v), f"fixture:{symbol}", "2026-11-02T14:45:00Z")

    def fetch_dividend_yield(self, underlying: str) -> Quote | None:
        return Quote(0.0125, "fixture:div", "2026-11-02T14:45:00Z")
