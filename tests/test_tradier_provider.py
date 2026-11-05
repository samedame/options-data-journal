"""TradierProvider: parsing, host safety, honest labelling, per-call fallback, and the full snapshot pipeline.
Everything here is SYNTHETIC: Tradier JSON is generated from the synthetic Black-Scholes chain, never fetched."""

from __future__ import annotations

import time
from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from odj.snapshot.providers import tradier_provider as tp
from odj.snapshot.providers.base import ProviderError
from odj.snapshot.providers.synthetic import SyntheticProvider
from odj.snapshot.run import run_snapshot
from odj.snapshot.store import load_chains
from tests.helpers import utc

MON = date(2026, 11, 2)


def tradier_json(raw: Any, age_s: float = 3.0) -> dict[str, list[dict[str, Any]]]:
    """Tradier-shaped option dicts per expiry, from a synthetic RawChain."""
    out: dict[str, list[dict[str, Any]]] = {}
    for r in raw.options.itertuples():
        stamp = int(time.time() * 1000) - int(age_s * 1000)
        out.setdefault(r.expiry.date().isoformat(), []).append(
            {
                "symbol": f"SPY{r.expiry:%y%m%d}{r.right}{int(r.strike * 1000):08d}",
                "strike": r.strike,
                "option_type": "call" if r.right == "C" else "put",
                "bid": r.bid,
                "ask": r.ask,
                "last": r.last,
                "volume": r.volume,
                "open_interest": r.open_interest,
                "trade_date": stamp,
                "bid_date": stamp,
                "ask_date": stamp,
                "greeks": {"mid_iv": r.iv_vendor},
            }
        )
    return out


class FakeTradier:
    def __init__(
        self, raw: Any, spot: float = 540.0, age_s: float = 3.0, fail: bool = False
    ) -> None:
        self.by_exp = tradier_json(raw, age_s)
        self.spot, self.age_s, self.fail = spot, age_s, fail
        self.calls: list[tuple[str, Mapping[str, str]]] = []

    def __call__(self, path: str, params: Mapping[str, str]) -> dict[str, Any]:
        self.calls.append((path, dict(params)))
        if self.fail:
            raise ProviderError("Tradier HTTP 500")
        stamp = int(time.time() * 1000) - int(self.age_s * 1000)
        if path == "/markets/quotes":
            return {"quotes": {"quote": {"symbol": "SPY", "last": self.spot, "trade_date": stamp}}}
        if path == "/markets/options/expirations":
            return {"expirations": {"date": sorted(self.by_exp)}}
        if path == "/markets/options/chains":
            return {"options": {"option": self.by_exp.get(params["expiration"])}}
        raise AssertionError(path)


@pytest.fixture
def synth() -> SyntheticProvider:
    return SyntheticProvider(spot=540.0, now=lambda: utc(MON, "09:46"), today=MON, n_expiries=10)


def make(synth: SyntheticProvider, **kw: Any) -> tuple[tp.TradierProvider, FakeTradier, list[date]]:
    exps = synth.expirations("SPY")[:4]
    fake = FakeTradier(synth.fetch_chain("SPY", exps), **kw)
    prov = tp.TradierProvider(
        fallback=synth, fetch=fake, attempts=1, sleep=lambda s: None, env={"TRADIER_API_KEY": "x"}
    )
    return prov, fake, exps


def test_chain_parsed_in_the_raw_schema(synth: SyntheticProvider) -> None:
    prov, fake, exps = make(synth)
    raw = prov.fetch_chain("SPY", exps)
    assert raw.provider == "tradier" and raw.underlying_price == 540.0
    assert list(raw.options.columns) == [
        "expiry", "strike", "right", "bid", "ask", "last", "last_trade_ts", "volume",
        "open_interest", "iv_vendor", "in_the_money",
    ]  # fmt: skip
    assert set(raw.options["right"]) == {"C", "P"}
    assert raw.options["iv_vendor"].between(0.01, 3).all()
    assert str(raw.options["last_trade_ts"].dt.tz) == "UTC"
    # in-the-money is computed from the spot: calls below it, puts above it
    calls = raw.options[raw.options["right"] == "C"]
    assert bool(calls[calls["strike"] < 540]["in_the_money"].all())
    assert not bool(calls[calls["strike"] > 540]["in_the_money"].any())
    assert all(c[1].get("greeks") == "true" for c in fake.calls if c[0].endswith("/chains"))


def test_real_quote_age_is_measured_not_assumed(synth: SyntheticProvider) -> None:
    prov, _, exps = make(synth, age_s=3.0)
    info = prov.fetch_chain("SPY", exps).info
    assert info["feed"] == "real time"
    assert 2.0 < info["newest_option_quote_age_s"] < 15.0
    assert 2.0 < info["underlying_quote_age_s"] < 15.0


def test_zero_trade_date_becomes_nat_and_missing_iv_nan(synth: SyntheticProvider) -> None:
    prov, fake, exps = make(synth)
    first = fake.by_exp[exps[0].isoformat()][0]
    first["trade_date"] = 0
    first["greeks"] = {"mid_iv": 0, "smv_vol": 0}
    raw = prov.fetch_chain("SPY", exps)
    assert raw.options["last_trade_ts"].isna().sum() == 1
    assert raw.options["iv_vendor"].isna().sum() == 1


def test_smv_vol_used_when_mid_iv_missing(synth: SyntheticProvider) -> None:
    prov, fake, exps = make(synth)
    fake.by_exp[exps[0].isoformat()][0]["greeks"] = {"mid_iv": None, "smv_vol": 0.31}
    raw = prov.fetch_chain("SPY", exps)
    assert 0.31 in set(raw.options["iv_vendor"].round(6))


def test_expirations_from_tradier(synth: SyntheticProvider) -> None:
    prov, fake, exps = make(synth)
    assert prov.expirations("SPY") == sorted(exps)
    assert [c[0] for c in fake.calls] == ["/markets/options/expirations"]


def test_chain_failure_falls_back_and_says_so(
    synth: SyntheticProvider, caplog: pytest.LogCaptureFixture
) -> None:
    prov, _, exps = make(synth, fail=True)
    with caplog.at_level("WARNING", logger="odj.tradier"):
        raw = prov.fetch_chain("SPY", exps)
        got = prov.expirations("SPY")
    assert raw.provider == "synthetic"  # the data's own feed is recorded, not "tradier"
    assert got == synth.expirations("SPY")
    assert sum("using synthetic" in r.message for r in caplog.records) == 2


def test_empty_chain_falls_back(synth: SyntheticProvider) -> None:
    prov, fake, exps = make(synth)
    fake.by_exp = {}
    assert prov.fetch_chain("SPY", exps).provider == "synthetic"


def test_no_fallback_raises_instead_of_inventing(synth: SyntheticProvider) -> None:
    exps = synth.expirations("SPY")[:2]
    fake = FakeTradier(synth.fetch_chain("SPY", exps), fail=True)
    prov = tp.TradierProvider(
        fetch=fake, attempts=1, sleep=lambda s: None, env={"TRADIER_API_KEY": "x"}
    )
    with pytest.raises(ProviderError):
        prov.fetch_chain("SPY", exps)
    assert prov.fetch_quote("^VIX") is None and prov.fetch_dividend_yield("SPY") is None


def test_vix_rate_and_dividend_stay_on_the_fallback(synth: SyntheticProvider) -> None:
    prov, fake, _ = make(synth)
    q = prov.fetch_quote("^VIX")
    assert q is not None and q.source == synth.fetch_quote("^VIX").source  # type: ignore[union-attr]
    assert prov.fetch_dividend_yield("SPY") is not None
    assert fake.calls == []


def test_sandbox_is_labelled_delayed(synth: SyntheticProvider) -> None:
    exps = synth.expirations("SPY")[:2]
    env = {"TRADIER_API_KEY": "x", "TRADIER_BASE_URL": tp.SANDBOX}
    prov = tp.TradierProvider(
        fallback=synth,
        fetch=FakeTradier(synth.fetch_chain("SPY", exps)),
        env=env,
        sleep=lambda s: None,
    )
    raw = prov.fetch_chain("SPY", exps)
    assert raw.provider == "tradier-sandbox" and "delayed" in raw.info["feed"]
    assert prov.versions()["tradier_host"] == "sandbox"


def test_host_allow_list_and_defaults() -> None:
    assert tp.base_url({}) == tp.PRODUCTION
    assert tp.base_url({"TRADIER_BASE_URL": "  "}) == tp.PRODUCTION
    assert tp.base_url({"TRADIER_ENV": "sandbox"}) == tp.SANDBOX
    assert tp.base_url({"TRADIER_BASE_URL": tp.PRODUCTION + "/"}) == tp.PRODUCTION
    with pytest.raises(ProviderError):
        tp.base_url({"TRADIER_BASE_URL": "https://evil.example.com/v1"})
    assert not tp.configured({"TRADIER_API_KEY": "  "}) and tp.configured({"TRADIER_TOKEN": "t"})
    with pytest.raises(ProviderError):
        tp.TradierProvider(env={})


def test_http_layer_is_market_data_only(synth: SyntheticProvider) -> None:
    prov = tp.TradierProvider(env={"TRADIER_API_KEY": "x"})
    with pytest.raises(ProviderError):
        prov._http("/accounts/123/balances", {})
    with pytest.raises(ProviderError):
        prov._http("/orders", {})


def test_full_snapshot_pipeline_on_tradier_data(root: Path, synth: SyntheticProvider) -> None:
    now = utc(MON, "09:46")
    exps = synth.expirations("SPY")
    fake = FakeTradier(synth.fetch_chain("SPY", exps))
    prov = tp.TradierProvider(
        fallback=SyntheticProvider(spot=540.0, now=lambda: now, today=MON, n_expiries=10),
        fetch=fake,
        attempts=1,
        sleep=lambda s: None,
        env={"TRADIER_API_KEY": "x"},
        now=lambda: now,
    )
    res = run_snapshot(root, provider=prov, now=now, underlyings=["SPY"], notify=False)
    assert res.results[0].status == "ok", res.results[0].error
    meta = (root / "data/chains/SPY/2026-11-02/0945.meta.json").read_text()
    assert '"provider": "tradier"' in meta and "tradier_host" in meta
    df = load_chains("SPY", MON, MON, usable_only=False, root=root)
    assert len(df) > 100 and df["delta"].notna().sum() > 50
    assert isinstance(df, pd.DataFrame)


def test_cli_provider_selection(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from odj import cli

    monkeypatch.delenv("TRADIER_API_KEY", raising=False)
    monkeypatch.delenv("TRADIER_TOKEN", raising=False)
    args = cli.build_parser().parse_args(["snapshot", "--dry-run"])
    monkeypatch.setenv("ODJ_PROVIDER", "tradier")
    assert cli.cmd_snapshot(args) == 2
    monkeypatch.setenv("ODJ_PROVIDER", "bogus")
    assert cli.cmd_snapshot(args) == 2
    assert "unknown ODJ_PROVIDER" in capsys.readouterr().err
