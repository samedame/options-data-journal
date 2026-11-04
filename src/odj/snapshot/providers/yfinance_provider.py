"""Default provider: Yahoo Finance via yfinance. Free, delayed, indicative - NOT an exchange feed."""

from __future__ import annotations

import logging
import time
from datetime import date
from typing import Any

import pandas as pd

from odj.common.retry import with_retries
from odj.common.timeutil import fmt_ts, now_utc
from odj.snapshot.columns import RAW_OPTION_COLUMNS
from odj.snapshot.providers.base import Provider, ProviderError, Quote, RawChain

log = logging.getLogger("odj.yfinance")


class YFinanceProvider(Provider):
    name = "yfinance"

    def __init__(self, attempts: int = 3, base_delay: float = 2.0, pause: float = 0.25):
        import yfinance as yf  # imported lazily so tests / selftest never touch the network stack

        self._yf = yf
        self.attempts = attempts
        self.base_delay = base_delay
        self.pause = pause
        self._tickers: dict[str, Any] = {}

    def _ticker(self, sym: str) -> Any:
        if sym not in self._tickers:
            self._tickers[sym] = self._yf.Ticker(sym)
        return self._tickers[sym]

    def versions(self) -> dict[str, str]:
        import importlib.metadata as md

        return {"yfinance": md.version("yfinance"), "pandas": pd.__version__}

    def _retry(self, fn: Any, what: str) -> Any:
        return with_retries(fn, attempts=self.attempts, base_delay=self.base_delay, what=what)

    def expirations(self, underlying: str) -> list[date]:
        opts = self._retry(
            lambda: tuple(self._ticker(underlying).options), f"{underlying} expiries"
        )
        if not opts:
            raise ProviderError(f"{underlying}: provider returned no expirations")
        return sorted(date.fromisoformat(s) for s in opts)

    def _spot(self, underlying: str) -> float:
        def go() -> float:
            t = self._ticker(underlying)
            try:
                v = float(t.fast_info["last_price"])
            except Exception:
                v = float("nan")
            if not v or v != v:
                hist = t.history(period="5d", interval="1d")["Close"].dropna()
                v = float(hist.iloc[-1])
            if not v or v != v or v <= 0:
                raise ProviderError(f"{underlying}: no usable spot price")
            return v

        return float(self._retry(go, f"{underlying} spot"))

    def fetch_chain(self, underlying: str, expiries: list[date]) -> RawChain:
        spot = self._spot(underlying)
        frames: list[pd.DataFrame] = []
        for exp in expiries:
            oc = self._retry(
                lambda exp=exp: self._ticker(underlying).option_chain(exp.isoformat()),
                f"{underlying} chain {exp}",
            )
            for right, df in (("C", oc.calls), ("P", oc.puts)):
                if df is None or len(df) == 0:
                    continue
                f = pd.DataFrame(
                    {
                        "expiry": pd.Timestamp(exp),
                        "strike": pd.to_numeric(df["strike"], errors="coerce"),
                        "right": right,
                        "bid": pd.to_numeric(df["bid"], errors="coerce"),
                        "ask": pd.to_numeric(df["ask"], errors="coerce"),
                        "last": pd.to_numeric(df["lastPrice"], errors="coerce"),
                        "last_trade_ts": pd.to_datetime(
                            df["lastTradeDate"], utc=True, errors="coerce"
                        ),
                        "volume": pd.to_numeric(df["volume"], errors="coerce"),
                        "open_interest": pd.to_numeric(df["openInterest"], errors="coerce"),
                        "iv_vendor": pd.to_numeric(df["impliedVolatility"], errors="coerce"),
                        "in_the_money": df["inTheMoney"].astype("boolean"),
                    }
                )
                frames.append(f)
            time.sleep(self.pause)
        if not frames:
            raise ProviderError(f"{underlying}: provider returned an empty chain")
        opts = pd.concat(frames, ignore_index=True)[RAW_OPTION_COLUMNS]
        return RawChain(
            underlying=underlying,
            underlying_price=spot,
            fetched_at=now_utc(),
            options=opts,
            provider=self.name,
            info={"expiries_requested": [e.isoformat() for e in expiries]},
        )

    def fetch_quote(self, symbol: str) -> Quote | None:
        def go() -> float:
            t = self._ticker(symbol)
            try:
                v = float(t.fast_info["last_price"])
            except Exception:
                v = float("nan")
            if not v or v != v:
                hist = t.history(period="5d", interval="1d")["Close"].dropna()
                v = float(hist.iloc[-1])
            return v

        try:
            v = float(self._retry(go, f"quote {symbol}"))
        except Exception as exc:
            log.warning("quote %s unavailable: %s", symbol, exc)
            return None
        if v != v:
            return None
        return Quote(value=v, source=f"yfinance:{symbol}", ts_utc=fmt_ts(now_utc()))

    def fetch_dividend_yield(self, underlying: str) -> Quote | None:
        """Trailing annual dividend yield as a fraction. Yahoo reports this inconsistently (fraction
        vs percent); values above 0.25 are treated as percent and divided by 100, and anything still
        outside [0, 0.25] is rejected so the caller falls back to the configured default."""
        try:
            info = self._retry(lambda: dict(self._ticker(underlying).info), f"{underlying} info")
        except Exception as exc:
            log.warning("dividend yield %s unavailable: %s", underlying, exc)
            return None
        for key in ("trailingAnnualDividendYield", "yield", "dividendYield"):
            raw = info.get(key)
            if raw is None:
                continue
            try:
                v = float(raw)
            except (TypeError, ValueError):
                continue
            if v > 0.25:
                v /= 100.0
            if 0.0 <= v <= 0.25 and v == v:
                return Quote(value=v, source=f"yfinance:info.{key}", ts_utc=fmt_ts(now_utc()))
        return None
