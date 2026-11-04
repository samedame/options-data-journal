"""Weekly report: markdown + JSON in reports/weekly/, and a compact Discord summary."""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from odj.common.config import find_root, journal_cfg
from odj.common.discord import post as discord_post
from odj.common.io import atomic_write_text, write_json
from odj.common.timeutil import ET, fmt_ts, now_utc, parse_ts
from odj.journal import metrics, store
from odj.journal.tracking import build_rows


def week_label(now: datetime) -> str:
    iso = now.astimezone(ET).isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def weekly_window_ok(now: datetime, cfg: dict[str, Any]) -> bool:
    """True if `now` (ET) is inside the configured Sunday-evening window."""
    w = cfg["report"]["weekly"]
    local = now.astimezone(ET)
    if local.strftime("%a").upper() != str(w["dow"]).upper()[:3]:
        return False
    h, m = (int(x) for x in str(w["time"]).split(":"))
    start = local.replace(hour=h, minute=m, second=0, microsecond=0)
    return start <= local <= start + timedelta(minutes=int(w["window_minutes"]))


def _f(x: Any, spec: str = ".2f", dash: str = "n/a") -> str:
    return dash if x is None else format(x, spec)


def _tag(m: dict[str, Any]) -> str:
    return f"n={m['n']}" + (" ⚠ INSUFFICIENT SAMPLE" if m.get("insufficient_sample") else "")


def render_markdown(
    label: str,
    now: datetime,
    mode: str,
    window: int,
    m: dict[str, Any],
    week: dict[str, int],
    cfg: dict[str, Any],
) -> str:
    sve = m["slippage_vs_edge"]
    L: list[str] = [f"# Execution-quality report {label}", ""]
    L.append(
        f"_Generated {fmt_ts(now)} · mark mode `{mode}` · trailing {window} days · min_sample {m['min_sample']}_"
    )
    L.append("")
    flags: list[str] = []
    if sve["red_flag"]:
        flags.append(
            f"🔴 **Slippage-vs-edge {sve['ratio']:.0%}** > {sve['warn_threshold']:.0%}: {sve['plain_english']}"
        )
    if m["unmarkable"]["count"]:
        flags.append(
            f"🟠 {m['unmarkable']['count']} alert(s) unmarkable by the shadow tracker: {m['unmarkable']['reasons']}"
        )
    L += ["## Flags", *(flags or ["None."]), ""]
    L += [
        "## This week",
        f"- alerts {week['alerts']} · fills {week['fills']} · skips {week['skips']} · exits {week['exits']}",
        "",
    ]
    tr = m["take_rate"]
    L += [
        "## Take rate",
        f"- overall: {tr['fills']}/{tr['n']} = {_f(tr['take_rate'] and tr['take_rate'] * 100, '.1f')}% ({_tag(tr)})",
        f"- skip reasons: {tr['skip_reasons'] or 'none'}",
        "",
        "| bot | alerts | fills | take rate | |",
        "|---|---|---|---|---|",
    ]
    for bot, t in m["take_rate_by_bot"].items():
        L.append(
            f"| {bot} | {t['n']} | {t['fills']} | {_f(t['take_rate'] and t['take_rate'] * 100, '.0f')}% | {_tag(t)} |"
        )
    es = m["entry_slippage"]
    L += [
        "",
        "## Entry slippage (negative = worse than alert)",
        f"- {_tag(es)}",
        f"- mean ${_f(es['mean_usd_per_contract'])}/contract (median ${_f(es['median_usd_per_contract'])}); "
        f"{_f(es['mean_pct_of_alert_premium'], '.1f')}% of alert premium; {_f(es['mean_pct_of_max_loss'], '.1f')}% of max loss",
    ]
    lat = m["latency"]
    L += [
        "",
        "## Latency alert→fill",
        f"- median {_f(lat['median_s'], '.0f')}s · p90 {_f(lat['p90_s'], '.0f')}s ({_tag(lat)})",
    ]
    ex = m["exit_slippage"]
    L += [
        "",
        "## Exit slippage vs shadow price at trigger",
        f"- mean ${_f(ex['mean_usd_per_contract'])}/contract ({_tag(ex)})",
    ]
    cr = m["capture_ratio"]
    L += [
        "",
        "## Capture ratio (realized after fees / theoretical before fees)",
        f"- overall {_f(cr['capture_ratio'], '.2f')} ({_tag(cr)}); realized ${_f(cr['realized_usd'])} vs theoretical ${_f(cr['theoretical_usd'])}",
    ]
    for title, key in (
        ("bot", "capture_ratio_by_bot"),
        ("strategy", "capture_ratio_by_strategy"),
        ("structure", "capture_ratio_by_structure"),
        ("underlying", "capture_ratio_by_underlying"),
    ):
        if m[key]:
            L += ["", f"| {title} | capture | | |", "|---|---|---|---|"]
            for k, v in m[key].items():
                L.append(f"| {k} | {_f(v['capture_ratio'], '.2f')} | {_tag(v)} | |")
    L += ["", "## Slippage vs edge", f"- {_tag(sve)}: {sve['plain_english'] or 'not enough data'}"]
    cal = m["calibration"]
    L += [
        "",
        "## Confidence-score calibration",
        f"- {_tag(cal)}",
        f"- **{cal['verdict']}**",
        "",
        "| score bucket | n | win rate | mean theoretical $ | mean realized $ (n) |",
        "|---|---|---|---|---|",
    ]
    for b in cal["buckets"]:
        L.append(
            f"| {b['bucket']} | {b['n']}{' ⚠' if b['insufficient_sample'] else ''} | {_f(b['win_rate'] and b['win_rate'] * 100, '.0f')}% | "
            f"{_f(b['mean_theoretical_pnl'])} | {_f(b['mean_realized_pnl'])} ({b['n_realized']}) |"
        )
    ge = m["gate_effect"]
    L += ["", "## Regime-gate effect"]
    if not ge["populated"]:
        L.append("- `regime_gate_state` not populated on any alert in this window.")
    for state, g in ge["by_state"].items():
        L.append(
            f"- gate `{state}`: {_tag(g)}, win rate {_f(g['win_rate'] and g['win_rate'] * 100, '.0f')}%, "
            f"mean theoretical ${_f(g['mean_theoretical_pnl'])}, mean realized ${_f(g['mean_realized_pnl'])}"
        )
    L += ["", "---", f"_{' '.join(str(cfg['report']['footer']).split())}_", ""]
    return "\n".join(L)


def generate_weekly(
    root: Path | None = None,
    *,
    now: datetime | None = None,
    mark_mode: str | None = None,
    notify: bool = True,
    force: bool = False,
) -> dict[str, Any]:
    root = root or find_root()
    cfg = journal_cfg(root)
    now = now or now_utc()
    label = week_label(now)
    md_path = root / "reports" / "weekly" / f"{label}.md"
    if md_path.exists() and not force:
        return {"status": "exists", "week": label, "path": str(md_path.relative_to(root))}
    window = int(cfg["report"]["window_days"])
    mode = mark_mode or cfg.get("default_mark_mode", "mid")
    rows, _ = build_rows(root, now=now, window_days=window, mark_mode=mode)  # type: ignore[arg-type]
    m = metrics.compute_all(rows, cfg)
    cutoff = now - timedelta(days=7)

    def n_week(kind: str) -> int:
        return sum(1 for r in store.read_all(root, kind) if parse_ts(r["ts_utc"]) >= cutoff)

    week = {
        "alerts": n_week("alert"),
        "fills": n_week("fill"),
        "skips": n_week("skip"),
        "exits": n_week("exit"),
    }
    md = render_markdown(label, now, mode, window, m, week, cfg)
    atomic_write_text(md_path, md)
    doc = {
        "week": label,
        "generated_at_utc": fmt_ts(now),
        "mark_mode": mode,
        "window_days": window,
        "this_week": week,
        "metrics": m,
    }
    write_json(root / "reports" / "weekly" / f"{label}.json", doc)
    if notify:
        discord_post(
            discord_summary(label, m, week), "warn" if m["slippage_vs_edge"]["red_flag"] else "info"
        )
    return {
        "status": "written",
        "week": label,
        "path": str(md_path.relative_to(root)),
        "metrics": m,
    }


def discord_summary(label: str, m: dict[str, Any], week: dict[str, int]) -> str:
    tr, es, cr, sve = m["take_rate"], m["entry_slippage"], m["capture_ratio"], m["slippage_vs_edge"]
    parts = [
        f"**Execution report {label}**",
        f"take rate {_f(tr['take_rate'] and tr['take_rate'] * 100, '.0f')}% ({tr['fills']}/{tr['n']}, {_tag(tr)})",
        f"avg entry slippage ${_f(es['mean_usd_per_contract'])}/contract ({_tag(es)})",
        f"capture ratio {_f(cr['capture_ratio'], '.2f')} ({_tag(cr)})",
        f"slippage-vs-edge {_f(sve['ratio'] and sve['ratio'] * 100, '.0f')}% ({_tag(sve)})",
        f"unmarkable alerts: {m['unmarkable']['count']}",
        f"this week: {week['alerts']} alerts / {week['fills']} fills / {week['skips']} skips",
    ]
    if sve["red_flag"]:
        parts.append(f"🔴 {sve['plain_english']}")
    parts.append("_delayed Yahoo data, twice-daily marks: approximate_")
    return "\n".join(parts)
