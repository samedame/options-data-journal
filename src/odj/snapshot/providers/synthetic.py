"""SYNTHETIC provider for tests and `odj selftest` ONLY. Generates a clearly fake, deterministic
chain from Black-Scholes with a skew. Never used for real data: `odj snapshot` refuses it unless
ODJ_ALLOW_SYNTHETIC=1 is set."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pandas as pd

from odj.snapshot.bs import bs_price
from odj.snapshot.columns import RAW_OPTION_COLUMNS
from odj.snapshot.providers.base import Provider, ProviderError, Quote, RawChain

SYNTHETIC_MARKER = "SYNTHETIC"


def third_friday_like(start: date, n: int) -> list[date]:
    """Deterministic list of weekday expiries: every Friday for n weeks plus the next 3 weekdays."""
    out: set[date] = set()
    d = start
    while len(out) < n + 3:
        if d.weekday() in (0, 2, 4):  # Mon/Wed/Fri like SPY
            out.add(d)
        d += timedelta(days=1)
    return sorted(out)


class SyntheticProvider(Provider):
    name = "synthetic"

    def __init__(
        self,
        spot: float | Callable[[str], float] = 540.0,
        now: Callable[[], datetime] | None = None,
        today: date | None = None,
        base_iv: float = 0.18,
        skew: float = -0.35,
        step: float = 1.0,
        half_spread: float = 0.02,
        fail_chain: bool = False,
        zero_bids: bool = False,
        with_market: bool = True,
        with_dividend: bool = True,
        n_expiries: int = 14,
    ):
        self._spot = spot
        self._now = now or (lambda: datetime.now(UTC))
        self.today = today
        self.base_iv = base_iv
        self.skew = skew
        self.step = step
        self.half_spread = half_spread
        self.fail_chain = fail_chain
        self.zero_bids = zero_bids
        self.with_market = with_market
        self.with_dividend = with_dividend
        self.n_expiries = n_expiries

    def spot(self, underlying: str) -> float:
        return float(self._spot(underlying)) if callable(self._spot) else float(self._spot)

    def expirations(self, underlying: str) -> list[date]:
        t = self.today or self._now().date()
        return third_friday_like(t, self.n_expiries)

    def fetch_chain(self, underlying: str, expiries: list[date]) -> RawChain:
        if self.fail_chain:
            raise ProviderError("synthetic provider configured to fail")
        now = self._now()
        S = self.spot(underlying)
        r, q = 0.04, 0.0125
        rows: list[pd.DataFrame] = []
        strikes = np.arange(np.floor(S * 0.80), np.ceil(S * 1.20) + self.step, self.step)
        for e in expiries:
            close = datetime(e.year, e.month, e.day, 21, 0, tzinfo=UTC)  # ~16:00 ET
            T = max((close - now).total_seconds() / (365 * 86400), 1e-5)
            for right in ("C", "P"):
                iv = np.clip(self.base_iv + self.skew * np.log(strikes / S), 0.05, 2.0)
                mid = bs_price(S, strikes, T, r, q, iv, right == "C")
                half = np.maximum(self.half_spread, 0.01 * mid)
                bid = np.maximum(mid - half, 0.0).round(2)
                ask = (mid + half).round(2)
                if self.zero_bids:
                    bid = np.zeros_like(bid)
                seed = int(hashlib.sha256(f"{underlying}{e}{right}".encode()).hexdigest()[:8], 16)
                rng = np.random.default_rng(seed)
                rows.append(
                    pd.DataFrame(
                        {
                            "expiry": pd.Timestamp(e),
                            "strike": strikes,
                            "right": right,
                            "bid": bid,
                            "ask": ask,
                            "last": ((bid + ask) / 2).round(2),
                            "last_trade_ts": pd.Timestamp(now - timedelta(minutes=5)).tz_convert(
                                "UTC"
                            ),
                            "volume": rng.integers(0, 5000, len(strikes)).astype(float),
                            "open_interest": rng.integers(10, 50000, len(strikes)).astype(float),
                            "iv_vendor": (iv + rng.normal(0, 0.005, len(strikes))).round(5),
                            "in_the_money": pd.array([False] * len(strikes), dtype="boolean"),
                        }
                    )
                )
        opts = pd.concat(rows, ignore_index=True)[RAW_OPTION_COLUMNS]
        return RawChain(underlying, S, now, opts, self.name, {"marker": SYNTHETIC_MARKER})

    def fetch_quote(self, symbol: str) -> Quote | None:
        if not self.with_market:
            return None
        vals = {"^VIX": 16.0, "^VIX9D": 15.0, "^VIX3M": 18.0, "^VXN": 20.0, "^IRX": 4.0}
        if symbol not in vals:
            return None
        return Quote(
            vals[symbol], f"synthetic:{symbol}", self._now().strftime("%Y-%m-%dT%H:%M:%SZ")
        )

    def fetch_dividend_yield(self, underlying: str) -> Quote | None:
        if not self.with_dividend:
            return None
        return Quote(0.0125, "synthetic:dividend", self._now().strftime("%Y-%m-%dT%H:%M:%SZ"))
