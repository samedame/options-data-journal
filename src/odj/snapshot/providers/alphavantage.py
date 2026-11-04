"""Alpha Vantage HISTORICAL_OPTIONS provider - used ONLY by `odj backfill`.

UNVERIFIED-LIVE: written from Alpha Vantage's documented response shape; it has not been run
against a real key. If $ALPHAVANTAGE_API_KEY is absent, or the endpoint says the plan lacks access,
`odj backfill` prints a clear message and exits 0 (never fails CI).
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any

import pandas as pd
import requests

from odj.snapshot.columns import RAW_OPTION_COLUMNS


class AlphaVantageUnavailable(RuntimeError):
    """No key, no access, or the endpoint did not return option data."""


class AlphaVantageProvider:
    name = "alphavantage"

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str = "https://www.alphavantage.co/query",
        timeout: int = 30,
    ):
        self.api_key = api_key or os.environ.get("ALPHAVANTAGE_API_KEY", "")
        self.base_url = base_url
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.api_key)

    def fetch_day(self, underlying: str, d: date) -> tuple[float | None, pd.DataFrame]:
        """End-of-day chain for `d`. Returns (underlying_price or None, raw option rows)."""
        if not self.configured:
            raise AlphaVantageUnavailable("ALPHAVANTAGE_API_KEY is not set")
        resp = requests.get(
            self.base_url,
            params={
                "function": "HISTORICAL_OPTIONS",
                "symbol": underlying,
                "date": d.isoformat(),
                "apikey": self.api_key,
            },
            timeout=self.timeout,
        )
        resp.raise_for_status()
        payload: dict[str, Any] = resp.json()
        data = payload.get("data")
        if not data:
            note = (
                payload.get("Information")
                or payload.get("Note")
                or payload.get("Error Message")
                or payload.get("message")
            )
            raise AlphaVantageUnavailable(f"no option data for {underlying} {d}: {str(note)[:200]}")
        df = pd.DataFrame(data)
        need = {"expiration", "strike", "type"}
        if not need.issubset(df.columns):
            raise AlphaVantageUnavailable(f"unexpected response columns: {sorted(df.columns)}")

        def num(col: str) -> pd.Series:
            return (
                pd.to_numeric(df[col], errors="coerce")
                if col in df
                else pd.Series(float("nan"), index=df.index)
            )

        out = pd.DataFrame(
            {
                "expiry": pd.to_datetime(df["expiration"]),
                "strike": num("strike"),
                "right": df["type"].astype(str).str.upper().str[0],
                "bid": num("bid"),
                "ask": num("ask"),
                "last": num("last") if "last" in df else num("mark"),
                "last_trade_ts": pd.NaT,
                "volume": num("volume"),
                "open_interest": num("open_interest"),
                "iv_vendor": num("implied_volatility"),
                "in_the_money": pd.array([pd.NA] * len(df), dtype="boolean"),
            }
        )[RAW_OPTION_COLUMNS]
        out["last_trade_ts"] = pd.to_datetime(out["last_trade_ts"], utc=True)
        return None, out
