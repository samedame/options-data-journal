"""Small, dependency-light statistics helpers: percentiles, Spearman rank correlation, bootstrap CI."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any

import numpy as np
from scipy.stats import rankdata


def median(xs: Sequence[float]) -> float | None:
    return float(np.median(xs)) if len(xs) else None


def percentile(xs: Sequence[float], p: float) -> float | None:
    return float(np.percentile(xs, p)) if len(xs) else None


def mean(xs: Sequence[float]) -> float | None:
    return float(np.mean(xs)) if len(xs) else None


def spearman(x: Any, y: Any) -> float | None:
    """Spearman rank correlation (average ranks for ties). None if undefined (n<3 or constant)."""
    if len(x) != len(y) or len(x) < 3:
        return None
    rx, ry = rankdata(x), rankdata(y)
    if np.std(rx) == 0 or np.std(ry) == 0:
        return None
    return float(np.corrcoef(rx, ry)[0, 1])


def bootstrap_spearman_ci(
    x: Sequence[float],
    y: Sequence[float],
    *,
    resamples: int = 2000,
    ci: float = 0.95,
    seed: int = 20260101,
) -> dict[str, Any]:
    """Percentile-bootstrap CI for Spearman's rho (pairs resampled with replacement, fixed seed)."""
    n = len(x)
    rho = spearman(x, y)
    out: dict[str, Any] = {
        "n": n,
        "rho": rho,
        "ci_low": None,
        "ci_high": None,
        "ci_includes_zero": None,
    }
    if rho is None:
        return out
    xa, ya = np.asarray(x, dtype=float), np.asarray(y, dtype=float)
    rng = np.random.default_rng(seed)
    vals: list[float] = []
    for _ in range(resamples):
        idx = rng.integers(0, n, n)
        r = spearman(xa[idx], ya[idx])
        if r is not None and not math.isnan(r):
            vals.append(r)
    if len(vals) < max(50, resamples // 10):
        return out
    lo = (1 - ci) / 2 * 100
    out["ci_low"] = float(np.percentile(vals, lo))
    out["ci_high"] = float(np.percentile(vals, 100 - lo))
    out["ci_includes_zero"] = bool(out["ci_low"] <= 0.0 <= out["ci_high"])
    return out
