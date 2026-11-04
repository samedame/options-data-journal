"""Black-Scholes-Merton (continuous dividend yield) price, greeks and implied vol. Vectorized.

Conventions
-----------
* T is in years (ACT/365). r and q are continuously-compounded annual rates.
* ``theta`` is per CALENDAR DAY. ``vega`` is per 1 volatility POINT (i.e. d price / d sigma / 100).
* ``rho`` is not computed.
* Equity-ETF options (SPY/QQQ/IWM) are American-style; BSM is a European approximation. Early
  exercise matters mainly for deep-ITM puts / ex-dividend calls, which the data-quality flags
  (parity_violation, iv_divergence) tend to surface.

Degenerate inputs: sigma <= 0 or T <= 0 collapse to the discounted-forward intrinsic value, with
step-function delta and zero gamma/vega.
"""

from __future__ import annotations

from typing import Any

import numpy as np
from numpy.typing import ArrayLike, NDArray
from scipy.special import ndtr

FloatArr = NDArray[np.float64]
_SQRT_2PI = np.sqrt(2.0 * np.pi)


def _pdf(x: FloatArr) -> FloatArr:
    return np.asarray(np.exp(-0.5 * x * x) / _SQRT_2PI, dtype=np.float64)


def _prep(*args: ArrayLike) -> list[FloatArr]:
    return [np.asarray(a, dtype=np.float64) for a in np.broadcast_arrays(*args)]


def _d1_d2(
    S: FloatArr, K: FloatArr, T: FloatArr, r: FloatArr, q: FloatArr, sigma: FloatArr
) -> tuple[FloatArr, FloatArr, FloatArr]:
    """Return (d1, d2, sigma*sqrt(T)) with a well-defined +/-inf limit when sigma*sqrt(T) == 0."""
    Tp = np.where(T > 0, T, 0.0)
    vst = sigma * np.sqrt(Tp)
    fwd_log = np.log(S / K) + (r - q) * Tp
    with np.errstate(divide="ignore", invalid="ignore"):
        regular = (fwd_log + 0.5 * sigma * sigma * Tp) / vst
    sgn = np.sign(fwd_log)
    limit = np.where(sgn == 0, 0.0, np.copysign(np.inf, sgn))
    d1 = np.where(vst > 0, regular, limit)
    d2 = np.where(vst > 0, d1 - vst, d1)
    return d1, d2, vst


def bs_price(
    S: ArrayLike,
    K: ArrayLike,
    T: ArrayLike,
    r: ArrayLike,
    q: ArrayLike,
    sigma: ArrayLike,
    is_call: ArrayLike,
) -> FloatArr:
    S_, K_, T_, r_, q_, sig, c = _prep(S, K, T, r, q, sigma, is_call)
    call = c.astype(bool)
    d1, d2, _ = _d1_d2(S_, K_, T_, r_, q_, sig)
    Tp = np.where(T_ > 0, T_, 0.0)
    dq, dr = np.exp(-q_ * Tp), np.exp(-r_ * Tp)
    call_px = S_ * dq * ndtr(d1) - K_ * dr * ndtr(d2)
    put_px = K_ * dr * ndtr(-d2) - S_ * dq * ndtr(-d1)
    return np.asarray(np.where(call, call_px, put_px), dtype=np.float64)


def bs_greeks(
    S: ArrayLike,
    K: ArrayLike,
    T: ArrayLike,
    r: ArrayLike,
    q: ArrayLike,
    sigma: ArrayLike,
    is_call: ArrayLike,
) -> dict[str, FloatArr]:
    """Return {'delta','gamma','theta','vega'} (theta per calendar day, vega per vol point)."""
    S_, K_, T_, r_, q_, sig, c = _prep(S, K, T, r, q, sigma, is_call)
    call = c.astype(bool)
    d1, d2, vst = _d1_d2(S_, K_, T_, r_, q_, sig)
    Tp = np.where(T_ > 0, T_, 0.0)
    dq, dr = np.exp(-q_ * Tp), np.exp(-r_ * Tp)
    pdf1 = np.where(np.isfinite(d1), _pdf(np.where(np.isfinite(d1), d1, 0.0)), 0.0)

    delta = np.where(call, dq * ndtr(d1), -dq * ndtr(-d1))
    with np.errstate(divide="ignore", invalid="ignore"):
        gamma = np.where(vst > 0, dq * pdf1 / (S_ * vst), 0.0)
        decay = np.where(
            Tp > 0, -S_ * dq * pdf1 * sig / (2.0 * np.sqrt(np.where(Tp > 0, Tp, 1.0))), 0.0
        )
    theta_call = decay - r_ * K_ * dr * ndtr(d2) + q_ * S_ * dq * ndtr(d1)
    theta_put = decay + r_ * K_ * dr * ndtr(-d2) - q_ * S_ * dq * ndtr(-d1)
    theta = np.where(call, theta_call, theta_put) / 365.0
    vega = S_ * dq * pdf1 * np.sqrt(Tp) / 100.0
    return {
        "delta": np.asarray(delta, dtype=np.float64),
        "gamma": np.asarray(gamma, dtype=np.float64),
        "theta": np.asarray(theta, dtype=np.float64),
        "vega": np.asarray(vega, dtype=np.float64),
    }


def implied_vol(
    price: ArrayLike,
    S: ArrayLike,
    K: ArrayLike,
    T: ArrayLike,
    r: ArrayLike,
    q: ArrayLike,
    is_call: ArrayLike,
    *,
    lo: float = 1e-4,
    hi: float = 8.0,
    iters: int = 100,
    tol: float = 1e-9,
) -> FloatArr:
    """Implied volatility by vectorized bisection. NaN when T <= 0, price is NaN, or the price lies
    outside the no-arbitrage band [price(lo), price(hi)]."""
    px, S_, K_, T_, r_, q_, c = _prep(price, S, K, T, r, q, is_call)
    shape = px.shape
    lo_a = np.full(shape, lo)
    hi_a = np.full(shape, hi)
    p_lo = bs_price(S_, K_, T_, r_, q_, lo_a, c)
    p_hi = bs_price(S_, K_, T_, r_, q_, hi_a, c)
    valid = (
        np.isfinite(px) & (T_ > 0) & (S_ > 0) & (K_ > 0) & (px >= p_lo - tol) & (px <= p_hi + tol)
    )
    a, b = lo_a.copy(), hi_a.copy()
    for _ in range(iters):
        mid = 0.5 * (a + b)
        pm = bs_price(S_, K_, T_, r_, q_, mid, c)
        too_high = pm > px
        b = np.where(too_high, mid, b)
        a = np.where(too_high, a, mid)
        if float(np.max(b - a, initial=0.0)) < tol:
            break
    out = 0.5 * (a + b)
    # A price at/below the sigma=lo price means "vol ~ 0": not a meaningful implied vol.
    out = np.where(valid & (px > p_lo + tol), out, np.nan)
    return np.asarray(out, dtype=np.float64)


def as_scalar(x: Any) -> float:
    return float(np.asarray(x).reshape(-1)[0])
