"""Tradier provider: real-time option chains on a production key, with another provider as per-call fallback.

READ-ONLY by construction: only GET /markets/* is ever called, and the key is only ever sent to api.tradier.com or
sandbox.tradier.com (anything else in TRADIER_BASE_URL is rejected), so a mis-scoped key cannot place an order or leak.

What comes from Tradier: option expiries, the full chain (bid/ask/last, trade timestamps, volume, open interest, vendor
IV from the greeks block) and the underlying price. What does NOT: VIX-family and rate symbols and dividend yield stay
on the fallback (Yahoo), because Tradier does not serve ^IRX and index symbols vary by account; their `source` fields
still say "yfinance:..." so nothing is mislabelled.

Honesty rules: every RawChain says which feed produced it ("tradier", "tradier-sandbox" for the ~15-minute-delayed
sandbox, or the fallback's own name when Tradier failed), and a fallback is logged, never silent. A sandbox key works
but is delayed by design; use the production host for real-time data.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from datetime import date, datetime
from typing import Any

import pandas as pd

from odj.common.retry import with_retries
from odj.common.timeutil import now_utc
from odj.snapshot.columns import RAW_OPTION_COLUMNS
from odj.snapshot.providers.base import Provider, ProviderError, Quote, RawChain

log = logging.getLogger("odj.tradier")

PRODUCTION = "https://api.tradier.com/v1"
SANDBOX = "https://sandbox.tradier.com/v1"
Fetch = Callable[[str, Mapping[str, str]], dict[str, Any]]


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def token(env: Mapping[str, str] | None = None) -> str:
    e = _env(env)
    return (e.get("TRADIER_API_KEY") or e.get("TRADIER_TOKEN") or "").strip()


def configured(env: Mapping[str, str] | None = None) -> bool:
    return bool(token(env))


def base_url(env: Mapping[str, str] | None = None) -> str:
    """Production unless TRADIER_BASE_URL (or TRADIER_ENV=sandbox) says sandbox. Only the two Tradier hosts are allowed."""
    e = _env(env)
    raw = (e.get("TRADIER_BASE_URL") or "").strip().rstrip("/")
    if not raw:
        return SANDBOX if (e.get("TRADIER_ENV") or "").strip().lower() == "sandbox" else PRODUCTION
    if raw not in (PRODUCTION, SANDBOX):
        raise ProviderError(
            f"TRADIER_BASE_URL must be {PRODUCTION} or {SANDBOX} (got a different host; refusing to send the key)"
        )
    return raw


def _as_list(x: Any) -> list[Any]:
    return [] if x is None else x if isinstance(x, list) else [x]


def _num(v: Any) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return float("nan")


class TradierProvider(Provider):
    name = "tradier"

    def __init__(
        self,
        fallback: Provider | None = None,
        *,
        env: Mapping[str, str] | None = None,
        fetch: Fetch | None = None,
        attempts: int = 3,
        base_delay: float = 2.0,
        min_gap_s: float | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = now_utc,
    ) -> None:
        self._env_map = env
        self._now = now
        self.base = base_url(env)
        self.sandbox = self.base == SANDBOX
        if fetch is None and not configured(env):
            raise ProviderError("TRADIER_API_KEY is not set")
        self._fetch: Fetch = fetch or self._http
        self.fallback = fallback
        self.attempts, self.base_delay, self._sleep = attempts, base_delay, sleep
        # Tradier allows 120 (production) / 60 (sandbox) requests a minute.
        self._gap = min_gap_s if min_gap_s is not None else (1.1 if self.sandbox else 0.55)
        self._last = 0.0
        self.feed = "tradier-sandbox" if self.sandbox else "tradier"

    # ------------------------------------------------------------------ transport
    def _http(self, path: str, params: Mapping[str, str]) -> dict[str, Any]:
        if not path.startswith("/markets/"):
            raise ProviderError("read-only client: market data endpoints only")
        wait = self._gap - (time.monotonic() - self._last)
        if wait > 0:
            self._sleep(wait)
        self._last = time.monotonic()
        url = f"{self.base}{path}?{urllib.parse.urlencode(dict(params))}"
        req = urllib.request.Request(  # host is allow-listed in base_url()
            url,
            headers={
                "Authorization": f"Bearer {token(self._env_map)}",
                "Accept": "application/json",
                "User-Agent": "odj-snapshot/1.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                out = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise ProviderError(
                f"Tradier HTTP {exc.code} on {path}"
            ) from None  # never echo headers/token
        if not isinstance(out, dict):
            raise ProviderError(f"Tradier returned a non-object on {path}")
        return out

    def _get(self, path: str, params: Mapping[str, str], what: str) -> dict[str, Any]:
        return with_retries(
            lambda: self._fetch(path, params),
            attempts=self.attempts,
            base_delay=self.base_delay,
            sleep=self._sleep,
            what=what,
        )

    # ------------------------------------------------------------------ fallback plumbing
    def _fallback_or_raise(self, what: str, err: Exception) -> Provider:
        if self.fallback is None:
            raise ProviderError(
                f"Tradier {what} failed and no fallback is configured: {err}"
            ) from err
        log.warning(
            "Tradier %s unavailable (%s: %s); using %s",
            what,
            type(err).__name__,
            err,
            self.fallback.name,
        )
        return self.fallback

    def versions(self) -> dict[str, str]:
        v = {"tradier_api": "v1", "tradier_host": "sandbox" if self.sandbox else "production"}
        if self.fallback is not None:
            v.update(self.fallback.versions())
        return v

    # ------------------------------------------------------------------ Provider API
    def expirations(self, underlying: str) -> list[date]:
        try:
            d = self._get(
                "/markets/options/expirations", {"symbol": underlying}, f"{underlying} expiries"
            )
            out = sorted(
                date.fromisoformat(str(s))
                for s in _as_list((d.get("expirations") or {}).get("date"))
            )
            if not out:
                raise ProviderError(f"{underlying}: Tradier listed no expirations")
            return out
        except Exception as exc:
            return self._fallback_or_raise(f"{underlying} expirations", exc).expirations(underlying)

    def _spot(self, underlying: str) -> tuple[float, float | None]:
        d = self._get(
            "/markets/quotes", {"symbols": underlying, "greeks": "false"}, f"{underlying} quote"
        )
        qs = _as_list((d.get("quotes") or {}).get("quote"))
        q = qs[0] if qs else {}
        for k in ("last", "close"):
            v = _num(q.get(k))
            if v == v and v > 0:
                stamps = [_num(q.get(s)) for s in ("trade_date", "bid_date", "ask_date")]
                stamps = [x for x in stamps if x == x and x > 0]
                age = None if not stamps else max(0.0, time.time() - max(stamps) / 1000.0)
                return v, age
        raise ProviderError(f"{underlying}: Tradier returned no usable price")

    def _tradier_chain(self, underlying: str, expiries: list[date]) -> RawChain:
        spot, spot_age = self._spot(underlying)
        frames: list[pd.DataFrame] = []
        newest: float | None = None
        for exp in expiries:
            d = self._get(
                "/markets/options/chains",
                {"symbol": underlying, "expiration": exp.isoformat(), "greeks": "true"},
                f"{underlying} chain {exp}",
            )
            opts = [
                o for o in _as_list((d.get("options") or {}).get("option")) if o.get("option_type")
            ]
            if not opts:
                continue
            tdates = [_num(o.get("trade_date")) for o in opts]
            for o in opts:
                for k in ("trade_date", "bid_date", "ask_date"):
                    ms = _num(o.get(k))
                    if ms == ms and ms > 0 and (newest is None or ms > newest):
                        newest = ms
            right = ["C" if o["option_type"] == "call" else "P" for o in opts]
            strike = pd.Series([_num(o.get("strike")) for o in opts], dtype="float64")
            itm = pd.Series(
                [
                    (s < spot) if r == "C" else (s > spot)
                    for s, r in zip(strike, right, strict=True)
                ],
                dtype="boolean",
            )
            last_ts = pd.to_datetime(
                pd.Series([t if t == t and t > 0 else None for t in tdates], dtype="float64"),
                unit="ms",
                utc=True,
                errors="coerce",
            )
            frames.append(
                pd.DataFrame(
                    {
                        "expiry": pd.Timestamp(exp),
                        "strike": strike,
                        "right": right,
                        "bid": pd.Series([_num(o.get("bid")) for o in opts], dtype="float64"),
                        "ask": pd.Series([_num(o.get("ask")) for o in opts], dtype="float64"),
                        "last": pd.Series([_num(o.get("last")) for o in opts], dtype="float64"),
                        "last_trade_ts": last_ts,
                        "volume": pd.Series([_num(o.get("volume")) for o in opts], dtype="float64"),
                        "open_interest": pd.Series(
                            [_num(o.get("open_interest")) for o in opts], dtype="float64"
                        ),
                        "iv_vendor": pd.Series([self._iv(o) for o in opts], dtype="float64"),
                        "in_the_money": itm,
                    }
                )
            )
        if not frames:
            raise ProviderError(f"{underlying}: Tradier returned an empty chain")
        options = pd.concat(frames, ignore_index=True)[RAW_OPTION_COLUMNS]
        info: dict[str, Any] = {
            "expiries_requested": [e.isoformat() for e in expiries],
            "feed": "sandbox (~15 min delayed)" if self.sandbox else "real time",
            "underlying_quote_age_s": None if spot_age is None else round(spot_age, 1),
            "newest_option_quote_age_s": (
                None if newest is None else round(max(0.0, time.time() - newest / 1000.0), 1)
            ),
        }
        return RawChain(
            underlying=underlying,
            underlying_price=spot,
            fetched_at=self._now(),
            options=options,
            provider=self.feed,
            info=info,
        )

    @staticmethod
    def _iv(o: Mapping[str, Any]) -> float:
        g = o.get("greeks") or {}
        for k in ("mid_iv", "smv_vol"):
            v = _num(g.get(k))
            if v == v and v > 0:
                return v
        return float("nan")

    def fetch_chain(self, underlying: str, expiries: list[date]) -> RawChain:
        try:
            return self._tradier_chain(underlying, expiries)
        except Exception as exc:
            return self._fallback_or_raise(f"{underlying} chain", exc).fetch_chain(
                underlying, expiries
            )

    def fetch_quote(self, symbol: str) -> Quote | None:
        # VIX-family and the T-bill rate stay on the fallback (see module docstring).
        return self.fallback.fetch_quote(symbol) if self.fallback is not None else None

    def fetch_dividend_yield(self, underlying: str) -> Quote | None:
        return self.fallback.fetch_dividend_yield(underlying) if self.fallback is not None else None
