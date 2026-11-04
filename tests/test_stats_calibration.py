"""Rank correlation, bootstrap CI, calibration buckets, INSUFFICIENT SAMPLE handling, metrics wiring."""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from odj.common.config import journal_cfg
from odj.journal import metrics
from odj.journal.stats import bootstrap_spearman_ci, percentile, spearman

CFG = journal_cfg()
BOOT = {"resamples": 500, "ci": 0.95, "seed": 7}
BUCKETS = CFG["confidence_buckets"]


def rows_from(scores: np.ndarray, pnl: np.ndarray) -> list[dict[str, Any]]:
    return [
        {
            "alert_id": f"a{i}",
            "confidence_score": float(s),
            "shadow_status": "closed",
            "theoretical_pnl": float(p),
            "bot": "b",
        }
        for i, (s, p) in enumerate(zip(scores, pnl, strict=True))
    ]


def test_spearman_basics() -> None:
    assert spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1.0)
    assert spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1.0)
    assert spearman([1, 2, 3, 4, 5], [1, 2, 2, 4, 5]) == pytest.approx(
        0.9746794, abs=1e-6
    )  # ties -> average ranks
    assert spearman([1, 1, 1], [1, 2, 3]) is None  # constant
    assert spearman([1, 2], [1, 2]) is None  # n < 3


def test_known_correlation_is_recovered_and_ci_excludes_zero() -> None:
    rng = np.random.default_rng(1)
    n = 300
    scores = rng.uniform(1, 100, n)
    pnl = 2.0 * scores + rng.normal(0, 40, n)
    out = bootstrap_spearman_ci(scores, pnl, resamples=500, seed=3)
    assert 0.6 < out["rho"] < 0.95
    assert out["ci_includes_zero"] is False and out["ci_low"] > 0.5


def test_pure_noise_ci_includes_zero_and_verdict_says_so() -> None:
    rng = np.random.default_rng(11)
    n = 120
    rows = rows_from(rng.uniform(1, 100, n), rng.normal(0, 50, n))
    cal = metrics.calibration(rows, BUCKETS, BOOT, min_sample=30)
    assert cal["rank_correlation"]["ci_includes_zero"] is True
    assert "NOT demonstrated" in cal["verdict"]
    assert cal["insufficient_sample"] is False


def test_informative_score_verdict() -> None:
    rng = np.random.default_rng(5)
    s = rng.uniform(1, 100, 200)
    cal = metrics.calibration(rows_from(s, s + rng.normal(0, 15, 200)), BUCKETS, BOOT, 30)
    assert cal["rank_correlation"]["ci_includes_zero"] is False
    assert "associated with outcomes" in cal["verdict"]


def test_insufficient_sample_triggers_and_blocks_conclusions() -> None:
    cal = metrics.calibration(
        rows_from(np.arange(1, 11) * 9.0, np.arange(10) * 5.0), BUCKETS, BOOT, min_sample=30
    )
    assert cal["insufficient_sample"] is True and cal["label"] == "INSUFFICIENT SAMPLE"
    assert cal["verdict"].startswith("INSUFFICIENT SAMPLE")
    assert all(b["insufficient_sample"] for b in cal["buckets"] if b["n"] < 30)
    at_threshold = metrics.calibration(
        rows_from(np.linspace(1, 100, 30), np.linspace(-5, 5, 30)), BUCKETS, BOOT, min_sample=30
    )
    assert at_threshold["insufficient_sample"] is False  # n == min_sample is enough


def test_bucket_assignment_and_stats() -> None:
    rows = rows_from(
        np.array([10, 40, 41, 60, 61, 80, 81, 100.0]), np.array([-1, 2, 3, -4, 5, 6, -7, 8.0])
    )
    cal = metrics.calibration(rows, BUCKETS, BOOT, 1)
    by = {b["bucket"]: b for b in cal["buckets"]}
    assert [by[k]["n"] for k in ("1-40", "41-60", "61-80", "81-100")] == [2, 2, 2, 2]
    assert by["1-40"]["win_rate"] == 0.5 and by["1-40"]["mean_theoretical_pnl"] == pytest.approx(
        0.5
    )
    assert by["81-100"]["mean_theoretical_pnl"] == pytest.approx(0.5)


def test_bootstrap_is_deterministic_for_a_seed() -> None:
    x, y = list(range(40)), [v + (v % 7) for v in range(40)]
    assert bootstrap_spearman_ci(x, y, resamples=200, seed=9) == bootstrap_spearman_ci(
        x, y, resamples=200, seed=9
    )


def test_percentile_and_empty() -> None:
    assert percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 90) == pytest.approx(9.1)
    assert percentile([], 90) is None


def _taken(i: int, slip: float, theo: float, real: float, bot: str = "b1") -> dict[str, Any]:
    return {
        "alert_id": f"a{i}",
        "bot": bot,
        "strategy": "s",
        "structure": "put_credit_spread",
        "underlying": "SPY",
        "decision": "filled",
        "entry_slip": {
            "per_share": slip / 100,
            "usd_per_contract": slip,
            "pct_of_alert_premium": slip,
            "pct_of_max_loss": slip / 4,
        },
        "round_trip_slip_usd": -2 * slip,
        "shadow_status": "closed",
        "theoretical_pnl": theo,
        "realized_pnl_usd": real * 2,
        "contracts_closed": 2,
        "latency_s": 60.0 + i,
        "regime_gate_state": "on" if i % 2 else "off",
    }


def test_capture_ratio_slippage_vs_edge_and_labels() -> None:
    rows = [_taken(i, slip=-5.0, theo=20.0, real=10.0) for i in range(40)]
    m = metrics.compute_all(rows, CFG)
    assert m["capture_ratio"]["capture_ratio"] == pytest.approx(
        0.5
    )  # 10 realized vs 20 theoretical per contract
    assert m["capture_ratio"]["n"] == 40 and m["capture_ratio"]["insufficient_sample"] is False
    sve = m["slippage_vs_edge"]
    assert sve["ratio"] == pytest.approx(10.0 / 20.0)  # round-trip cost $10 vs edge $20
    assert sve["red_flag"] is False  # 0.5 is not > 0.5
    assert m["entry_slippage"]["mean_usd_per_contract"] == pytest.approx(-5.0)
    assert m["latency"]["median_s"] == pytest.approx(79.5)
    worse = metrics.compute_all([_taken(i, slip=-8.0, theo=20.0, real=5.0) for i in range(40)], CFG)
    assert worse["slippage_vs_edge"]["red_flag"] is True
    assert "eating 80%" in worse["slippage_vs_edge"]["plain_english"]
    small = metrics.compute_all(rows[:5], CFG)
    assert small["capture_ratio"]["label"] == "INSUFFICIENT SAMPLE"
    assert (
        small["slippage_vs_edge"]["red_flag"] is False
    )  # never red-flag on an insufficient sample


def test_take_rate_skip_reasons_and_gate_effect() -> None:
    rows = [_taken(i, -1.0, 10.0, 5.0) for i in range(6)]
    rows += [
        {"alert_id": f"s{i}", "bot": "b1", "decision": "skipped", "skip_reason": "capital"}
        for i in range(3)
    ]
    rows += [{"alert_id": "n", "bot": "b1", "decision": "none"}]
    tr = metrics.take_rate(rows, 30)
    assert tr["n"] == 10 and tr["fills"] == 6 and tr["skips"] == 3 and tr["undecided"] == 1
    assert tr["take_rate"] == pytest.approx(0.6) and tr["skip_reasons"] == {"capital": 3}
    ge = metrics.gate_effect(rows, 3)
    assert (
        ge["populated"] and set(ge["by_state"]) == {"on", "off"} and ge["by_state"]["on"]["n"] == 3
    )
    assert metrics.gate_effect([{"alert_id": "x"}], 30)["populated"] is False


def test_negative_theoretical_edge_has_no_ratio() -> None:
    m = metrics.compute_all([_taken(i, -1.0, -10.0, -12.0) for i in range(40)], CFG)
    assert m["capture_ratio"]["capture_ratio"] is None and m["slippage_vs_edge"]["ratio"] is None
