"""Edge-survival metrics. Pure functions over "trade rows" (one row per alert, see tracking.build_rows).

Every metric reports its sample size `n`. If n < min_sample the metric is labelled
INSUFFICIENT SAMPLE and must not be used to draw conclusions.

Row keys used here (all optional except alert_id):
  alert_id, bot, strategy, structure, underlying, ts_utc, confidence_score, regime_gate_state,
  decision ('filled'|'skipped'|'none'), skip_reason,
  entry_slip{per_share,usd_per_contract,pct_of_alert_premium,pct_of_max_loss}, latency_s,
  shadow_status ('closed'|'open'|'unmarkable'), theoretical_pnl (USD/contract, before fees),
  realized_pnl_usd (closed positions, after fees), contracts_closed, exit_slip{...},
  round_trip_slip_usd (positive = cost)
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Iterable
from typing import Any

from odj.journal.stats import bootstrap_spearman_ci, mean, median, percentile

Row = dict[str, Any]
LABEL = "INSUFFICIENT SAMPLE"


def _wrap(n: int, min_sample: int, **vals: Any) -> dict[str, Any]:
    low = n < min_sample
    return {"n": n, "insufficient_sample": low, "label": LABEL if low else None, **vals}


def group_by(rows: Iterable[Row], key: str) -> dict[str, list[Row]]:
    out: dict[str, list[Row]] = defaultdict(list)
    for r in rows:
        out[str(r.get(key) if r.get(key) is not None else "unknown")].append(r)
    return dict(out)


def take_rate(rows: list[Row], min_sample: int) -> dict[str, Any]:
    n = len(rows)
    filled = sum(1 for r in rows if r.get("decision") == "filled")
    skipped = [r for r in rows if r.get("decision") == "skipped"]
    undecided = n - filled - len(skipped)
    reasons = Counter(str(r.get("skip_reason") or "other") for r in skipped)
    return _wrap(
        n,
        min_sample,
        fills=filled,
        skips=len(skipped),
        undecided=undecided,
        take_rate=(filled / n) if n else None,
        skip_reasons=dict(sorted(reasons.items())),
    )


def entry_slippage(rows: list[Row], min_sample: int) -> dict[str, Any]:
    s = [r["entry_slip"] for r in rows if r.get("entry_slip")]
    n = len(s)

    def col(k: str) -> list[float]:
        return [float(x[k]) for x in s if x.get(k) is not None]

    return _wrap(
        n,
        min_sample,
        mean_per_share=mean(col("per_share")),
        mean_usd_per_contract=mean(col("usd_per_contract")),
        median_usd_per_contract=median(col("usd_per_contract")),
        mean_pct_of_alert_premium=mean(col("pct_of_alert_premium")),
        mean_pct_of_max_loss=mean(col("pct_of_max_loss")),
        note="negative = worse than the alert",
    )


def latency(rows: list[Row], min_sample: int) -> dict[str, Any]:
    xs = [float(r["latency_s"]) for r in rows if r.get("latency_s") is not None]
    return _wrap(
        len(xs),
        min_sample,
        median_s=median(xs),
        p90_s=percentile(xs, 90),
        unit="seconds alert->fill",
    )


def exit_slippage(rows: list[Row], min_sample: int) -> dict[str, Any]:
    s = [r["exit_slip"] for r in rows if r.get("exit_slip")]
    usd = [float(x["usd_per_contract"]) for x in s]
    return _wrap(
        len(s),
        min_sample,
        mean_usd_per_contract=mean(usd),
        median_usd_per_contract=median(usd),
        note="actual exit vs shadow-tracker price at rule trigger; negative = worse",
    )


def _paired(rows: list[Row]) -> list[Row]:
    return [
        r
        for r in rows
        if r.get("realized_pnl_usd") is not None
        and r.get("theoretical_pnl") is not None
        and r.get("contracts_closed")
    ]


def capture_ratio(rows: list[Row], min_sample: int) -> dict[str, Any]:
    """sum(realized $, after fees) / sum(theoretical $, before fees) over closed, taken, markable trades."""
    p = _paired(rows)
    realized = sum(float(r["realized_pnl_usd"]) for r in p)
    theo = sum(float(r["theoretical_pnl"]) * int(r["contracts_closed"]) for r in p)
    ratio = (realized / theo) if theo > 0 else None
    return _wrap(
        len(p),
        min_sample,
        realized_usd=realized if p else None,
        theoretical_usd=theo if p else None,
        capture_ratio=ratio,
        note=None
        if theo > 0 or not p
        else "theoretical edge in this group is not positive; ratio undefined",
    )


def slippage_vs_edge(rows: list[Row], min_sample: int, warn: float) -> dict[str, Any]:
    p = [
        r
        for r in rows
        if r.get("round_trip_slip_usd") is not None and r.get("theoretical_pnl") is not None
    ]
    cost = mean([float(r["round_trip_slip_usd"]) for r in p])
    edge = mean([float(r["theoretical_pnl"]) for r in p])
    ratio = (cost / edge) if (cost is not None and edge is not None and edge > 0) else None
    red = bool(ratio is not None and ratio > warn and len(p) >= min_sample)
    msg = None
    if ratio is not None:
        msg = (
            f"Execution is eating {ratio:.0%} of the theoretical edge per trade "
            f"(avg round-trip slippage ${cost:.2f} vs avg edge ${edge:.2f} per contract)."
        )
        if ratio > warn:
            msg += f" That exceeds the {warn:.0%} warning threshold."
    elif edge is not None and edge <= 0:
        msg = "Average theoretical edge is not positive in this sample, so the ratio is undefined."
    return _wrap(
        len(p),
        min_sample,
        avg_round_trip_slippage_cost_usd=cost,
        avg_theoretical_edge_usd=edge,
        ratio=ratio,
        warn_threshold=warn,
        red_flag=red,
        plain_english=msg,
    )


def calibration(
    rows: list[Row], buckets: list[list[int]], boot: dict[str, Any], min_sample: int
) -> dict[str, Any]:
    scored = [
        r
        for r in rows
        if r.get("confidence_score") is not None
        and r.get("shadow_status") == "closed"
        and r.get("theoretical_pnl") is not None
    ]
    out_buckets: list[dict[str, Any]] = []
    for lo, hi in buckets:
        b = [r for r in scored if lo <= float(r["confidence_score"]) <= hi]
        th = [float(r["theoretical_pnl"]) for r in b]
        rl = [
            float(r["realized_pnl_usd"]) / int(r["contracts_closed"])
            for r in b
            if r.get("realized_pnl_usd") is not None and r.get("contracts_closed")
        ]
        out_buckets.append(
            _wrap(
                len(b),
                min_sample,
                bucket=f"{lo}-{hi}",
                win_rate=(sum(1 for x in th if x > 0) / len(th)) if th else None,
                mean_theoretical_pnl=mean(th),
                mean_realized_pnl=mean(rl),
                n_realized=len(rl),
            )
        )
    x = [float(r["confidence_score"]) for r in scored]
    y = [float(r["theoretical_pnl"]) for r in scored]
    corr = bootstrap_spearman_ci(
        x, y, resamples=int(boot["resamples"]), ci=float(boot["ci"]), seed=int(boot["seed"])
    )
    low = len(scored) < min_sample
    if low:
        verdict = f"{LABEL}: n={len(scored)} < {min_sample}; no conclusion about the score."
    elif corr["rho"] is None or corr["ci_low"] is None:
        verdict = "Correlation undefined (constant scores or outcomes)."
    elif corr["ci_includes_zero"]:
        verdict = (
            f"The {int(float(boot['ci']) * 100)}% CI for the rank correlation includes zero "
            f"(rho={corr['rho']:.2f}, CI [{corr['ci_low']:.2f}, {corr['ci_high']:.2f}]): the score is "
            "NOT demonstrated to carry information about outcomes."
        )
    else:
        verdict = (
            f"The CI excludes zero (rho={corr['rho']:.2f}, CI [{corr['ci_low']:.2f}, {corr['ci_high']:.2f}]): "
            "the score is associated with outcomes in this sample."
        )
    return _wrap(
        len(scored), min_sample, buckets=out_buckets, rank_correlation=corr, verdict=verdict
    )


def gate_effect(rows: list[Row], min_sample: int) -> dict[str, Any]:
    groups = group_by(
        [r for r in rows if r.get("regime_gate_state") is not None], "regime_gate_state"
    )
    out: dict[str, Any] = {}
    for state, g in sorted(groups.items()):
        th = [
            float(r["theoretical_pnl"])
            for r in g
            if r.get("shadow_status") == "closed" and r.get("theoretical_pnl") is not None
        ]
        rl = [
            float(r["realized_pnl_usd"]) / int(r["contracts_closed"])
            for r in g
            if r.get("realized_pnl_usd") is not None and r.get("contracts_closed")
        ]
        out[state] = _wrap(
            len(th),
            min_sample,
            win_rate=(sum(1 for x in th if x > 0) / len(th)) if th else None,
            mean_theoretical_pnl=mean(th),
            mean_realized_pnl=mean(rl),
            n_realized=len(rl),
        )
    return {"populated": bool(out), "by_state": out}


def grouped(rows: list[Row], key: str, fn: Callable[[list[Row]], dict[str, Any]]) -> dict[str, Any]:
    return {k: fn(v) for k, v in sorted(group_by(rows, key).items())}


def compute_all(rows: list[Row], cfg: dict[str, Any]) -> dict[str, Any]:
    ms = int(cfg["min_sample"])
    warn = float(cfg["slippage_edge_warn"])
    unmarkable = [r for r in rows if r.get("shadow_status") == "unmarkable"]

    def cap(rs: list[Row]) -> dict[str, Any]:
        return capture_ratio(rs, ms)

    return {
        "alerts": len(rows),
        "min_sample": ms,
        "take_rate": take_rate(rows, ms),
        "take_rate_by_bot": grouped(rows, "bot", lambda rs: take_rate(rs, ms)),
        "take_rate_by_strategy": grouped(rows, "strategy", lambda rs: take_rate(rs, ms)),
        "entry_slippage": entry_slippage(rows, ms),
        "entry_slippage_by_bot": grouped(rows, "bot", lambda rs: entry_slippage(rs, ms)),
        "latency": latency(rows, ms),
        "exit_slippage": exit_slippage(rows, ms),
        "capture_ratio": cap(rows),
        "capture_ratio_by_bot": grouped(rows, "bot", cap),
        "capture_ratio_by_strategy": grouped(rows, "strategy", cap),
        "capture_ratio_by_structure": grouped(rows, "structure", cap),
        "capture_ratio_by_underlying": grouped(rows, "underlying", cap),
        "slippage_vs_edge": slippage_vs_edge(rows, ms, warn),
        "calibration": calibration(rows, cfg["confidence_buckets"], cfg["bootstrap"], ms),
        "gate_effect": gate_effect(rows, ms),
        "unmarkable": {
            "count": len(unmarkable),
            "reasons": dict(Counter(str(r.get("shadow_reason") or "unknown") for r in unmarkable)),
            "alert_ids": [r["alert_id"] for r in unmarkable][:50],
        },
    }
