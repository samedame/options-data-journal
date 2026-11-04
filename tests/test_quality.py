"""Every quality flag has a positive and a negative test; parity has known violating/clean chains."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from odj.snapshot.bs import bs_price
from odj.snapshot.quality import DEFAULT_QUALITY, apply_flags, summarize

SNAP = pd.Timestamp("2026-11-02T14:45:00Z")


def row(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "bid": 1.00,
        "ask": 1.04,
        "mid": 1.02,
        "last_trade_ts": SNAP - pd.Timedelta(minutes=3),
        "iv_vendor": 0.20,
        "iv_calc": 0.205,
        "open_interest": 100.0,
        "underlying_price": 540.0,
        "strike": 530.0,
        "right": "P",
        "expiry": pd.Timestamp("2026-11-20"),
        "_T": 18 / 365,
        "r_used": 0.04,
        "q_used": 0.0125,
    }
    base.update(over)
    return base


def flags(**over: object) -> pd.Series:
    df = pd.DataFrame([row(**over)])
    return apply_flags(df, DEFAULT_QUALITY, SNAP).iloc[0]


def test_clean_row_has_no_flags_and_is_usable() -> None:
    r = flags()
    assert not r[
        [
            "zero_bid",
            "zero_ask",
            "crossed_market",
            "wide_spread",
            "stale_last",
            "iv_divergence",
            "missing_oi",
        ]
    ].any()
    assert bool(r["usable"])


def test_zero_bid() -> None:
    assert bool(flags(bid=0.0, ask=0.05, mid=0.025)["zero_bid"])
    assert not bool(flags()["zero_bid"])


def test_zero_ask() -> None:
    r = flags(bid=0.0, ask=0.0, mid=np.nan)
    assert bool(r["zero_ask"]) and not bool(r["usable"])
    assert not bool(flags()["zero_ask"])


def test_crossed_market() -> None:
    assert bool(flags(bid=1.10, ask=1.00, mid=np.nan)["crossed_market"])
    assert not bool(flags()["crossed_market"])


def test_wide_spread_needs_both_pct_and_absolute() -> None:
    assert bool(flags(bid=0.50, ask=1.50, mid=1.0)["wide_spread"])  # 100% and $1.00
    assert not bool(flags(bid=0.01, ask=0.05, mid=0.03)["wide_spread"])  # 133% but only $0.04
    assert not bool(flags()["wide_spread"])


def test_stale_last() -> None:
    assert bool(flags(last_trade_ts=SNAP - pd.Timedelta(hours=5))["stale_last"])
    assert bool(flags(last_trade_ts=pd.NaT)["stale_last"])
    assert not bool(flags()["stale_last"])


def test_iv_divergence() -> None:
    assert bool(flags(iv_vendor=0.60, iv_calc=0.20)["iv_divergence"])
    assert not bool(flags(iv_vendor=0.21, iv_calc=0.20)["iv_divergence"])
    assert not bool(flags(iv_calc=np.nan)["iv_divergence"])  # undefined, not "divergent"


def test_missing_oi() -> None:
    assert bool(flags(open_interest=np.nan)["missing_oi"])
    assert not bool(flags(open_interest=0.0)["missing_oi"])  # zero OI is data, not missing


def _parity_chain(put_bump: float = 0.0) -> pd.DataFrame:
    S, r, q, T, sig = 540.0, 0.04, 0.0125, 30 / 365, 0.2
    rows = []
    for K in (520.0, 530.0, 540.0, 550.0):
        for right in ("C", "P"):
            mid = float(bs_price(S, K, T, r, q, sig, right == "C"))
            if right == "P" and K == 540.0:
                mid += put_bump
            rows.append(
                row(
                    strike=K,
                    right=right,
                    mid=mid,
                    bid=mid - 0.02,
                    ask=mid + 0.02,
                    underlying_price=S,
                    _T=T,
                    r_used=r,
                    q_used=q,
                    iv_calc=sig,
                    iv_vendor=sig,
                )
            )
    return pd.DataFrame(rows)


def test_parity_violation_flags_only_the_broken_pair() -> None:
    clean = apply_flags(_parity_chain(), DEFAULT_QUALITY, SNAP)
    assert not clean["parity_violation"].any()
    bad = apply_flags(_parity_chain(put_bump=1.50), DEFAULT_QUALITY, SNAP)
    flagged = bad[bad["parity_violation"]]
    assert set(flagged["strike"]) == {540.0} and set(flagged["right"]) == {"C", "P"}
    assert int(bad["parity_violation"].sum()) == 2


def test_parity_skips_pairs_without_two_sided_quotes() -> None:
    df = _parity_chain(put_bump=5.0)
    df.loc[(df["strike"] == 540.0) & (df["right"] == "P"), "bid"] = 0.0
    out = apply_flags(df, DEFAULT_QUALITY, SNAP)
    assert not out["parity_violation"].any()


def test_summary_counts() -> None:
    df = apply_flags(
        pd.DataFrame([row(), row(bid=0.0, ask=0.05, mid=0.025), row(bid=0, ask=0, mid=np.nan)]),
        DEFAULT_QUALITY,
        SNAP,
    )
    s = summarize(df)
    assert s["rows"] == 3 and s["usable_rows"] == 1
    assert s["flag_counts"]["zero_bid"] == 2 and s["flag_counts"]["zero_ask"] == 1
    assert s["usable_pct"] == pytest.approx(33.33, abs=0.01)
