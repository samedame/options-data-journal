"""Provider interface. Only YFinanceProvider (default) and AlphaVantageProvider (backfill) are real;
SyntheticProvider exists solely for tests / `odj selftest`."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import pandas as pd


class ProviderError(RuntimeError):
    """The provider failed or returned an obviously broken payload."""


@dataclass(frozen=True)
class Quote:
    value: float
    source: str
    ts_utc: str


@dataclass
class RawChain:
    """Unfiltered provider output for the requested expiries.

    `options` columns follow columns.RAW_OPTION_COLUMNS; `expiry` is a date-like, `right` is 'C'/'P',
    `last_trade_ts` is tz-aware UTC (or NaT).
    """

    underlying: str
    underlying_price: float
    fetched_at: datetime
    options: pd.DataFrame
    provider: str
    info: dict[str, Any] = field(default_factory=dict)


class Provider(ABC):
    name: str = "abstract"

    @abstractmethod
    def expirations(self, underlying: str) -> list[date]:
        """All listed expiries for the underlying, ascending."""

    @abstractmethod
    def fetch_chain(self, underlying: str, expiries: list[date]) -> RawChain:
        """Fetch the full (unfiltered) chain for the given expiries."""

    @abstractmethod
    def fetch_quote(self, symbol: str) -> Quote | None:
        """Latest value for an index/rate symbol such as ^VIX. None if unavailable."""

    def fetch_dividend_yield(self, underlying: str) -> Quote | None:
        """Annual dividend yield as a fraction (0.013 = 1.3%). None if unavailable."""
        return None

    def versions(self) -> dict[str, str]:
        return {}
