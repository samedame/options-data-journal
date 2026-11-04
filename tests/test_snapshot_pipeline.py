"""Snapshot pipeline: idempotency, windows, defaults, failure handling, pinning, compaction, export."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from odj.common import heartbeat
from odj.common.calendars import market_is_open
from odj.common.config import tickers_cfg
from odj.journal import store
from odj.snapshot.columns import CHAIN_COLUMNS
from odj.snapshot.compact import compact_all, compact_month, size_report
from odj.snapshot.export import export_chains
from odj.snapshot.providers.base import ProviderError, RawChain
from odj.snapshot.providers.synthetic import SyntheticProvider
from odj.snapshot.run import check_chain, run_snapshot
from odj.snapshot.store import load_chains, load_market, snapshot_index
from tests.helpers import FIXTURES, FixtureProvider, alert, utc

MON = date(2026, 11, 2)


def synth(now: datetime, **kw: Any) -> SyntheticProvider:
    return SyntheticProvider(spot=540.0, now=lambda: now, today=now.date(), n_expiries=10, **kw)


def tree_hash(p: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(p.rglob("*")):
        if f.is_file() and not f.name.endswith(".lock"):
            h.update(str(f.relative_to(p)).encode())
            h.update(f.read_bytes())
    return h.hexdigest()


def test_recorded_fixture_chain_snapshot(root: Path) -> None:
    now = utc(MON, "09:46")
    res = run_snapshot(
        root,
        provider=FixtureProvider(FIXTURES / "chain_SYNTHETIC_spy_open.json", now),
        now=now,
        underlyings=["SPY"],
        notify=False,
    )
    r = res.results[0]
    assert res.slot == "open" and r.status == "ok" and r.rows == 200
    pq = root / "data/chains/SPY/2026-11-02/0945.parquet"
    meta = json.loads((root / "data/chains/SPY/2026-11-02/0945.meta.json").read_text())
    assert (
        pq.exists()
        and meta["rows"] == 200
        and meta["slot"] == "open"
        and meta["provider"] == "fixture"
    )
    assert meta["greeks_inputs_defaulted"] is False and meta["r_used"] == pytest.approx(0.04)
    assert {"python", "pandas", "pyarrow"} <= set(meta["library_versions"])
    assert set(meta["flag_counts"]) >= {"zero_bid", "crossed_market", "parity_violation"}
    df = load_chains("SPY", MON, MON, usable_only=False, root=root)
    assert list(df.columns) == [*CHAIN_COLUMNS, "snapshot_date"]
    assert len(df) == 200 and df["delta"].notna().sum() > 150
    # put deltas negative, call deltas positive, sane IV for a liquid ATM option
    atm = df[(df["strike"] == 540.0) & (df["expiry"] == "2026-11-20")]
    assert atm[atm["right"] == "C"]["delta"].iloc[0] > 0 > atm[atm["right"] == "P"]["delta"].iloc[0]
    assert 0.10 < float(atm["iv_calc"].iloc[0]) < 0.30
    mkt = load_market(MON, MON, root=root)
    assert float(mkt["vix"].iloc[0]) == 16.0 and float(mkt["rate"].iloc[0]) == pytest.approx(0.04)
    assert not bool(mkt["rate_defaulted"].iloc[0]) and float(
        mkt["div_yield_SPY"].iloc[0]
    ) == pytest.approx(0.0125)
    hb = heartbeat.load(root)["jobs"]["snapshot"]
    assert hb["last_status"] == "ok" and hb["last_slot"] == "2026-11-02/open"


def test_same_slot_twice_is_byte_identical_noop(root: Path) -> None:
    now = utc(MON, "09:46")
    run_snapshot(root, provider=synth(now), now=now, underlyings=["SPY"], notify=False)
    before = tree_hash(root / "data")
    again = run_snapshot(
        root,
        provider=synth(utc(MON, "09:58")),
        now=utc(MON, "09:58"),
        underlyings=["SPY"],
        notify=False,
    )
    assert [r.status for r in again.results] == ["skipped_exists"]
    assert tree_hash(root / "data") == before


def test_outside_window_and_closed_days_do_nothing(root: Path) -> None:
    for now in (
        utc(MON, "11:30"),
        utc(date(2026, 11, 26), "09:50"),
        utc(date(2026, 11, 28), "09:50"),
    ):
        res = run_snapshot(root, provider=synth(now), now=now, notify=False)
        assert res.skipped_reason and not res.results
    assert not (root / "data").exists()


def test_half_day_late_slot_runs_at_1250_and_not_at_1555(root: Path) -> None:
    d = date(2026, 11, 27)
    ok = run_snapshot(
        root,
        provider=synth(utc(d, "12:55")),
        now=utc(d, "12:55"),
        underlyings=["SPY"],
        notify=False,
    )
    assert ok.slot == "close" and (root / "data/chains/SPY/2026-11-27/1250.parquet").exists()
    late = run_snapshot(
        root,
        provider=synth(utc(d, "15:55")),
        now=utc(d, "15:55"),
        underlyings=["SPY"],
        notify=False,
    )
    assert late.skipped_reason


def test_defaulted_greeks_inputs_are_flagged_loudly(root: Path) -> None:
    now = utc(MON, "09:46")
    res = run_snapshot(
        root,
        provider=synth(now, with_market=False, with_dividend=False),
        now=now,
        underlyings=["SPY"],
        notify=False,
    )
    assert res.results[0].status == "ok"
    assert any("defaulted" in w for w in res.warnings)
    meta = json.loads((root / "data/chains/SPY/2026-11-02/0945.meta.json").read_text())
    assert meta["greeks_inputs_defaulted"] is True and meta["r_source"].startswith("config:")
    assert bool(load_market(MON, MON, root=root)["rate_defaulted"].iloc[0])


def test_defaulted_inputs_post_a_discord_warning(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "odj.snapshot.run.discord_post", lambda msg, level="info": sent.append((level, msg)) or True
    )
    now = utc(MON, "09:46")
    run_snapshot(root, provider=synth(now, with_market=False), now=now, underlyings=["SPY"])
    assert any(level == "warn" and "defaulted" in msg for level, msg in sent)


def test_broken_chain_is_rejected_not_stored_and_alerts(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "odj.snapshot.run.discord_post", lambda msg, level="info": sent.append((level, msg)) or True
    )
    now = utc(MON, "10:00")
    assert market_is_open(now)
    res = run_snapshot(root, provider=synth(now, zero_bids=True), now=now, underlyings=["SPY"])
    r = res.results[0]
    assert r.status == "failed" and "broken chain" in (r.error or "")
    assert not list((root / "data").rglob("*.parquet")) and not res.ok
    assert any(level == "fail" for level, _ in sent)
    assert heartbeat.load(root)["jobs"]["snapshot"]["last_status"] == "failed"


def test_one_failing_underlying_does_not_block_others(root: Path) -> None:
    class Flaky(SyntheticProvider):
        def fetch_chain(self, underlying: str, expiries: list[date]) -> RawChain:
            if underlying == "QQQ":
                raise ProviderError("boom")
            return super().fetch_chain(underlying, expiries)

    now = utc(MON, "09:46")
    res = run_snapshot(
        root,
        provider=Flaky(spot=540.0, now=lambda: now, today=now.date(), n_expiries=6),
        now=now,
        underlyings=["SPY", "QQQ"],
        notify=False,
    )
    assert {r.underlying: r.status for r in res.results} == {
        "SPY": "ok",
        "QQQ": "failed",
    } and not res.ok
    assert (root / "data/chains/SPY/2026-11-02/0945.parquet").exists()
    assert not (root / "data/chains/QQQ").exists()


def test_empty_chain_is_a_failure() -> None:
    cfg = tickers_cfg()
    assert "zero rows" in (check_chain(pd.DataFrame(), cfg, True, min_rows=50) or "")
    small = pd.DataFrame(
        {
            "underlying_price": [540.0] * 3,
            "strike": [540.0] * 3,
            "dte": [1] * 3,
            "zero_bid": [False] * 3,
            "zero_ask": [False] * 3,
        }
    )
    assert "only 3 rows" in (check_chain(small, cfg, True, min_rows=50) or "")
    assert check_chain(small, cfg, True, min_rows=1) is None


def test_zero_bids_after_the_close_are_not_treated_as_broken() -> None:
    cfg = tickers_cfg()
    n = 100
    df = pd.DataFrame(
        {
            "underlying_price": [540.0] * n,
            "strike": [540.0] * n,
            "dte": [5] * n,
            "zero_bid": [True] * n,
            "zero_ask": [False] * n,
        }
    )
    assert check_chain(df, cfg, market_open=False, min_rows=50) is None
    assert "zero bid" in (check_chain(df, cfg, market_open=True, min_rows=50) or "")


def test_pinned_contracts_bypass_filters(root: Path) -> None:
    now = utc(MON, "09:46")
    run_snapshot(root, provider=synth(now), now=now, underlyings=["SPY"], notify=False, force=True)
    plain = load_chains("SPY", MON, MON, usable_only=False, root=root)
    assert not (
        (plain["strike"] == 440.0) & (plain["right"] == "P")
    ).any()  # 18.5% OTM: filtered out
    assert not plain["pinned"].any()
    leg = {"side": "sell", "type": "put", "strike": 440.0, "expiry": "2026-11-20", "ratio": 1}
    store.append(
        root,
        "alert",
        alert(
            legs=[leg, {**leg, "side": "buy", "strike": 439.0}],
            ts_utc="2026-11-02T14:30:00Z",
            id="spy-bps-bot-20261102T143000Z-1",
        ),
    )
    run_snapshot(root, provider=synth(now), now=now, underlyings=["SPY"], notify=False, force=True)
    pinned = load_chains("SPY", MON, MON, usable_only=False, root=root)
    hit = pinned[
        (pinned["strike"] == 440.0) & (pinned["right"] == "P") & (pinned["expiry"] == "2026-11-20")
    ]
    assert len(hit) == 1 and bool(hit["pinned"].iloc[0])
    assert int(pinned["pinned"].sum()) == 2


def test_pin_only_underlying_captures_just_the_pinned_rows(root: Path) -> None:
    store.append(
        root,
        "alert",
        alert(
            underlying="NVDA",
            id="spy-bps-bot-20261102T143100Z-1",
            ts_utc="2026-11-02T14:31:00Z",
            legs=[
                {"side": "buy", "type": "call", "strike": 540.0, "expiry": "2026-11-20", "ratio": 1}
            ],
            structure="long_call",
        ),
    )
    now = utc(MON, "09:46")
    res = run_snapshot(root, provider=synth(now), now=now, underlyings=None, notify=False)
    nv = next(r for r in res.results if r.underlying == "NVDA")
    assert nv.status == "ok" and nv.rows == 1


def test_dry_run_writes_nothing_but_reports_size(root: Path) -> None:
    now = utc(MON, "09:46")
    res = run_snapshot(
        root, provider=synth(now), now=now, underlyings=["SPY"], dry_run=True, notify=False
    )
    r = res.results[0]
    assert r.status == "dry_run" and r.rows > 1000 and (r.parquet_bytes or 0) > 10_000
    assert not (root / "data").exists() and not (root / "status").exists()


def test_load_chains_filters(root: Path) -> None:
    now = utc(MON, "09:46")
    run_snapshot(root, provider=synth(now), now=now, underlyings=["SPY"], notify=False)
    allr = load_chains("SPY", MON, MON, usable_only=False, root=root)
    use = load_chains("SPY", MON, MON, root=root)
    assert 0 < len(use) <= len(allr) and use["usable"].all()
    puts = load_chains("SPY", MON, MON, right="P", dte_min=10, dte_max=20, root=root)
    assert len(puts) > 0 and set(puts["right"]) == {"P"} and puts["dte"].between(10, 20).all()
    assert load_chains("SPY", date(2026, 12, 1), date(2026, 12, 2), root=root).empty


def test_compaction_roundtrip_is_lossless_and_idempotent(root: Path) -> None:
    days = [date(2026, 11, 2), date(2026, 11, 3), date(2026, 11, 4)]
    for d in days:
        now = utc(d, "09:46")
        run_snapshot(root, provider=synth(now), now=now, underlyings=["SPY"], notify=False)
    before = load_chains("SPY", days[0], days[-1], usable_only=False, root=root).reset_index(
        drop=True
    )
    res = compact_month(root, "SPY", "2026-11")
    assert res["compacted"] == 3 and res["daily_removed"] == 3
    assert (root / "data/chains/SPY/compacted/2026-11.parquet").exists()
    assert not list((root / "data/chains/SPY").glob("2026-*/*.parquet"))
    after = load_chains("SPY", days[0], days[-1], usable_only=False, root=root).reset_index(
        drop=True
    )
    pd.testing.assert_frame_equal(before, after)
    assert compact_month(root, "SPY", "2026-11")["status"] == "nothing to do"
    h = tree_hash(root / "data")
    again = run_snapshot(
        root,
        provider=synth(utc(days[0], "09:50")),
        now=utc(days[0], "09:50"),
        underlyings=["SPY"],
        notify=False,
    )
    assert [r.status for r in again.results] == ["skipped_exists"] and tree_hash(root / "data") == h
    assert len(snapshot_index(root, "SPY")) == 3
    # current month is never auto-compacted; completed months are
    assert compact_all(root, today=date(2026, 11, 15)) == []


def test_compact_all_picks_up_completed_months(root: Path) -> None:
    now = utc(date(2026, 10, 30), "09:46")
    run_snapshot(root, provider=synth(now), now=now, underlyings=["SPY"], notify=False)
    out = compact_all(root, today=date(2026, 11, 3))
    assert out and out[0]["month"] == "2026-10" and out[0]["status"] == "ok"


def test_export_csv_and_parquet(root: Path) -> None:
    now = utc(MON, "09:46")
    run_snapshot(root, provider=synth(now), now=now, underlyings=["SPY"], notify=False)
    n_expected = len(load_chains("SPY", MON, MON, root=root))
    p_csv, n_csv = export_chains("SPY", MON, MON, "csv", root=root)
    p_pq, n_pq = export_chains("SPY", MON, MON, "parquet", root=root)
    assert n_csv == n_pq == n_expected
    csv = pd.read_csv(p_csv)
    assert len(csv) == n_expected and csv["snapshot_ts_utc"].iloc[0].endswith("Z")
    assert len(pd.read_parquet(p_pq)) == n_expected
    with pytest.raises(ValueError, match="optionsdx"):
        export_chains("SPY", MON, MON, "optionsdx", root=root)


def test_size_report_and_limit(root: Path) -> None:
    now = utc(MON, "09:46")
    run_snapshot(root, provider=synth(now), now=now, underlyings=["SPY"], notify=False)
    rep = size_report(root)
    assert rep["data_mb"] > 0 and rep["over_limit"] is False
    tiny = size_report(
        root, {**tickers_cfg(root), "storage": {**tickers_cfg(root)["storage"], "size_warn_mb": 0}}
    )
    assert tiny["over_limit"] is True


def test_synthetic_provider_is_refused_by_the_cli_without_opt_in(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from odj.cli import main

    monkeypatch.setenv("ODJ_PROVIDER", "synthetic")
    monkeypatch.delenv("ODJ_ALLOW_SYNTHETIC", raising=False)
    assert main(["snapshot", "--dry-run"]) == 2
