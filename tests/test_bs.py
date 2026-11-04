"""Closed-form BSM greeks vs an INDEPENDENT finite-difference implementation, plus IV round trips."""

from __future__ import annotations

import itertools
import math

import numpy as np
import pytest

from odj.snapshot.bs import bs_greeks, bs_price, implied_vol

R, Q = 0.04, 0.013


def _n(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def ref_price(S: float, K: float, T: float, r: float, q: float, sig: float, call: bool) -> float:
    """Independent scalar implementation (math.erf, no scipy)."""
    if T <= 0 or sig <= 0:
        fwd = S * math.exp(-q * max(T, 0)) - K * math.exp(-r * max(T, 0))
        return max(fwd if call else -fwd, 0.0)
    d1 = (math.log(S / K) + (r - q + 0.5 * sig * sig) * T) / (sig * math.sqrt(T))
    d2 = d1 - sig * math.sqrt(T)
    if call:
        return S * math.exp(-q * T) * _n(d1) - K * math.exp(-r * T) * _n(d2)
    return K * math.exp(-r * T) * _n(-d2) - S * math.exp(-q * T) * _n(-d1)


def fd_greeks(S: float, K: float, T: float, sig: float, call: bool) -> dict[str, float]:
    # step must be small relative to the price kernel's width sigma*sqrt(T)*S, or FD truncation dominates
    hS = max(S * 1e-5, min(S * 1e-3, 0.05 * S * sig * math.sqrt(T)))
    hv, hT = 1e-4, T * 1e-3
    p = lambda S_=S, T_=T, v=sig: ref_price(S_, K, T_, R, Q, v, call)  # noqa: E731
    return {
        "delta": (p(S_=S + hS) - p(S_=S - hS)) / (2 * hS),
        "gamma": (p(S_=S + hS) - 2 * p() + p(S_=S - hS)) / hS**2,
        "vega": (p(v=sig + hv) - p(v=sig - hv)) / (2 * hv) / 100.0,
        "theta": -(p(T_=T + hT) - p(T_=T - hT)) / (2 * hT) / 365.0,
    }


GRID = list(
    itertools.product(
        [100.0],
        [50.0, 80.0, 95.0, 100.0, 105.0, 120.0, 200.0],  # deep ITM ... deep OTM
        [1 / 365, 7 / 365, 0.25, 1.0, 2.0],  # near expiry ... long dated
        [0.08, 0.2, 0.6, 1.5],
        [True, False],
    )
)


@pytest.mark.parametrize(("S", "K", "T", "sig", "call"), GRID)
def test_price_and_greeks_match_finite_differences(
    S: float, K: float, T: float, sig: float, call: bool
) -> None:
    px = float(bs_price(S, K, T, R, Q, sig, call))
    assert px == pytest.approx(ref_price(S, K, T, R, Q, sig, call), abs=1e-9)
    g = bs_greeks(S, K, T, R, Q, sig, call)
    fd = fd_greeks(S, K, T, sig, call)
    assert float(g["delta"]) == pytest.approx(fd["delta"], abs=5e-5)
    assert float(g["gamma"]) == pytest.approx(fd["gamma"], abs=2e-4, rel=2e-3)
    assert float(g["vega"]) == pytest.approx(fd["vega"], abs=1e-4, rel=1e-3)
    assert float(g["theta"]) == pytest.approx(fd["theta"], abs=2e-4, rel=2e-3)


def test_put_call_parity_identity() -> None:
    for S, K, T, sig in [(100, 90, 0.5, 0.3), (100, 100, 1.0, 0.2), (100, 130, 0.1, 0.5)]:
        c = float(bs_price(S, K, T, R, Q, sig, True))
        p = float(bs_price(S, K, T, R, Q, sig, False))
        assert c - p == pytest.approx(S * math.exp(-Q * T) - K * math.exp(-R * T), abs=1e-10)


def test_vectorized_matches_scalar() -> None:
    K = np.array([90.0, 100.0, 110.0])
    v = bs_price(100.0, K, 0.5, R, Q, 0.25, np.array([True, False, True]))
    for i, call in enumerate([True, False, True]):
        assert v[i] == pytest.approx(
            ref_price(100.0, float(K[i]), 0.5, R, Q, 0.25, call), abs=1e-10
        )


def test_zero_vol_collapses_to_discounted_intrinsic() -> None:
    T = 0.5
    fwd = 100 * math.exp(-Q * T) - 90 * math.exp(-R * T)
    assert float(bs_price(100, 90, T, R, Q, 0.0, True)) == pytest.approx(fwd)
    assert float(bs_price(100, 90, T, R, Q, 0.0, False)) == pytest.approx(0.0)
    g = bs_greeks(100, 90, T, R, Q, 0.0, True)
    assert float(g["delta"]) == pytest.approx(math.exp(-Q * T))
    assert float(g["gamma"]) == 0.0 and float(g["vega"]) == 0.0
    g_otm = bs_greeks(100, 110, T, R, Q, 0.0, True)
    assert float(g_otm["delta"]) == 0.0


def test_expiry_edge_cases() -> None:
    assert float(bs_price(105, 100, 0.0, R, Q, 0.3, True)) == pytest.approx(5.0)
    assert float(bs_price(95, 100, 0.0, R, Q, 0.3, True)) == pytest.approx(0.0)
    g = bs_greeks(105, 100, 0.0, R, Q, 0.3, True)
    assert float(g["delta"]) == pytest.approx(1.0) and float(g["gamma"]) == 0.0
    assert all(np.isfinite(v).all() for v in g.values())


@pytest.mark.parametrize(
    ("K", "T", "sig", "call"),
    [
        (K, T, s, c)
        for K in (85.0, 100.0, 115.0)
        for T in (7 / 365, 0.25, 1.0)
        for s in (0.1, 0.35, 0.9)
        for c in (True, False)
    ],
)
def test_iv_round_trip(K: float, T: float, sig: float, call: bool) -> None:
    px = float(bs_price(100.0, K, T, R, Q, sig, call))
    vega_pt = float(bs_greeks(100.0, K, T, R, Q, sig, call)["vega"])
    if px < 1e-6 or vega_pt < 5e-3:
        pytest.skip(
            "price is (nearly) insensitive to vol here: IV is ill-conditioned, by construction"
        )
    iv = float(implied_vol(px, 100.0, K, T, R, Q, call))
    assert iv == pytest.approx(sig, abs=2e-4, rel=2e-3)
    assert float(bs_price(100.0, K, T, R, Q, iv, call)) == pytest.approx(px, abs=1e-6)


def test_iv_rejects_impossible_prices() -> None:
    assert np.isnan(implied_vol(0.5, 100.0, 90.0, 0.5, R, Q, True))  # below intrinsic
    assert np.isnan(implied_vol(5.0, 100.0, 100.0, 0.0, R, Q, True))  # no time left
    assert np.isnan(implied_vol(float("nan"), 100.0, 100.0, 0.5, R, Q, True))
    assert np.isnan(implied_vol(150.0, 100.0, 100.0, 0.5, R, Q, True))  # above S: impossible
