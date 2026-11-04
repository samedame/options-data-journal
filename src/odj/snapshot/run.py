"""Snapshot orchestration: window/calendar checks, market data, per-underlying fetch -> enrich ->
sanity check -> atomic write (parquet + sidecar meta). Idempotent on (underlying, date, slot)."""

from __future__ import annotations

import importlib.metadata as md
import logging
import platform
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from odj.common import heartbeat
from odj.common.calendars import (
    SlotTime,
    in_slot_window,
    market_is_open,
    match_slot,
    slot_time,
)
from odj.common.config import core_and_extra, find_root, tickers_cfg
from odj.common.discord import post as discord_post
from odj.common.io import atomic_write_bytes, dumps, file_lock, read_json, write_json
from odj.common.timeutil import ET, et_datetime, fmt_ts, now_utc
from odj.snapshot.columns import MARKET_KEYS
from odj.snapshot.enrich import PinKey, build_chain
from odj.snapshot.providers.base import Provider, ProviderError, Quote, RawChain
from odj.snapshot.quality import summarize
from odj.snapshot.store import (
    daily_path,
    market_path,
    meta_path,
    snapshot_exists,
    write_parquet,
)

log = logging.getLogger("odj.snapshot")


@dataclass
class UResult:
    underlying: str
    status: str  # ok | skipped_exists | failed | dry_run
    rows: int = 0
    usable_pct: float | None = None
    parquet_bytes: int | None = None
    flag_counts: dict[str, int] = field(default_factory=dict)
    pinned_rows: int = 0
    error: str | None = None
    path: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class RunResult:
    slot: str | None
    date: date | None
    results: list[UResult] = field(default_factory=list)
    skipped_reason: str | None = None
    warnings: list[str] = field(default_factory=list)
    dry_run: bool = False

    @property
    def ok(self) -> bool:
        return all(r.status != "failed" for r in self.results)


def resolve_slot(
    cfg: dict[str, Any], now: datetime, slot_arg: str | None, ignore_window: bool
) -> tuple[SlotTime | None, str | None]:
    """Pick the slot to run, in New York time. Returns (slot, None) or (None, reason-to-skip)."""
    snap = cfg["snapshot"]
    window = int(snap["slot_window_minutes"])
    slots = snap["slots"]
    if slot_arg in (None, "", "auto"):
        st = match_slot(now, slots, window)
        if st is not None:
            return st, None
        if not ignore_window:
            return None, "outside every slot window (or not a trading day)"
        slot_arg = str(
            slots[0]["name"]
        )  # manual/dry run outside any window: default to the first slot
    sc = next((s for s in slots if s["name"] == slot_arg), None)
    if sc is None:
        raise ValueError(f"unknown slot {slot_arg!r}; configured: {[s['name'] for s in slots]}")
    d = now.astimezone(ET).date()
    st = slot_time(sc, d)
    if ignore_window:
        if st is None:  # non-trading day: allow manual/dry runs, using the nominal time
            st = SlotTime(sc["name"], d, et_datetime(d, str(sc["time"])))
        return st, None
    if st is None:
        return None, f"{d} is not a trading day"
    if not in_slot_window(now, st, window):
        return None, (
            f"now ({now.astimezone(ET):%H:%M} ET) is outside the '{slot_arg}' slot window "
            f"({st.at_et:%H:%M} ET + {window} min)"
        )
    return st, None


def collect_market(
    provider: Provider, cfg: dict[str, Any], underlyings: list[str]
) -> tuple[dict[str, Any], float, dict[str, float], list[str]]:
    """Fetch VIX-family, rate and dividend yields. Falls back to config defaults (flagged) for
    rate/yield; VIX-family gaps are recorded as null and warned about."""
    warnings: list[str] = []
    gd = cfg["greeks_defaults"]
    out: dict[str, Any] = {}
    for key in MARKET_KEYS:
        sym = cfg["market_symbols"][key]
        q = provider.fetch_quote(sym)
        if q is None:
            out[key] = {"value": None, "source": sym, "ts_utc": fmt_ts(now_utc()), "missing": True}
            if key != "rate":
                warnings.append(f"market symbol {sym} unavailable")
            continue
        val = q.value / 100.0 if key == "rate" else q.value
        out[key] = {"value": val, "source": q.source, "ts_utc": q.ts_utc}
    rate_v = (out["rate"] or {}).get("value")
    rate_defaulted = rate_v is None or not (0.0 <= float(rate_v) <= 0.25)
    if rate_defaulted:
        rate = float(gd["risk_free_rate"])
        out["rate"] = {
            "value": rate,
            "source": "config:greeks_defaults.risk_free_rate",
            "ts_utc": fmt_ts(now_utc()),
            "defaulted": True,
        }
        warnings.append(f"risk-free rate defaulted to {rate:.4f} (^IRX unavailable/invalid)")
    else:
        rate = float(rate_v) if rate_v is not None else float(gd["risk_free_rate"])
    divs: dict[str, float] = {}
    out["dividend_yield"] = {}
    for u in underlyings:
        dq: Quote | None = provider.fetch_dividend_yield(u)
        if dq is None:
            dv = float(gd["dividend_yield"].get(u, gd["dividend_yield"]["default"]))
            out["dividend_yield"][u] = {
                "value": dv,
                "source": "config:greeks_defaults.dividend_yield",
                "ts_utc": fmt_ts(now_utc()),
                "defaulted": True,
            }
            warnings.append(f"dividend yield for {u} defaulted to {dv:.4f}")
        else:
            dv = dq.value
            out["dividend_yield"][u] = {"value": dv, "source": dq.source, "ts_utc": dq.ts_utc}
        divs[u] = dv
    return out, rate, divs, warnings


def write_market_slot(
    root: Path, d: date, slot: str, slot_doc: dict[str, Any], force: bool
) -> bool:
    p = market_path(root, d)
    with file_lock(p):
        doc = read_json(p, default=None) or {"date": d.isoformat(), "slots": {}}
        if slot in doc["slots"] and not force:
            return False
        doc["slots"][slot] = slot_doc
        write_json(p, doc)
    return True


def check_chain(
    df: pd.DataFrame, cfg: dict[str, Any], market_open: bool, *, min_rows: int
) -> str | None:
    """Return a failure reason if the chain is obviously broken, else None."""
    bc = cfg["snapshot"]["broken_chain"]
    if len(df) == 0:
        return "zero rows after filtering"
    if len(df) < min_rows:
        return f"only {len(df)} rows (< {min_rows})"
    if not market_open:
        return None
    spot = float(df["underlying_price"].iloc[0])
    near = df[
        ((df["strike"] / spot - 1.0).abs() <= float(bc["near_money_pct"]) / 100.0)
        & (df["dte"] >= 1)
    ]
    if len(near) >= int(bc["min_near_money_rows"]):
        frac = float(near["zero_bid"].mean())
        if frac > float(bc["max_zero_bid_fraction"]):
            return (
                f"{frac:.0%} of near-the-money rows have zero bid during market hours "
                f"(> {float(bc['max_zero_bid_fraction']):.0%}); treating the chain as broken"
            )
        if bool(near["zero_ask"].all()):
            return "every near-the-money ask is zero during market hours"
    return None


def _versions(provider: Provider) -> dict[str, str]:
    v = {"python": platform.python_version(), "pandas": pd.__version__}
    for pkg in ("pyarrow", "numpy", "scipy", "odj"):
        try:
            v[pkg] = md.version("options-data-journal" if pkg == "odj" else pkg)
        except md.PackageNotFoundError:
            pass
    v.update(provider.versions())
    return v


def run_snapshot(
    root: Path | None = None,
    *,
    provider: Provider,
    slot: str | None = None,
    underlyings: list[str] | None = None,
    now: datetime | None = None,
    force: bool = False,
    dry_run: bool = False,
    ignore_window: bool = False,
    notify: bool = True,
) -> RunResult:
    root = root or find_root()
    cfg = tickers_cfg(root)
    now = now or now_utc()
    st, why = resolve_slot(cfg, now, slot, ignore_window)
    if st is None:
        log.info("snapshot skipped: %s", why)
        return RunResult(slot=slot, date=None, skipped_reason=why, dry_run=dry_run)

    from odj.journal.positions import pinned_contracts  # local import: journal depends on snapshot

    pins_by_und: dict[str, set[PinKey]] = (
        pinned_contracts(root, st.date) if cfg["underlyings"].get("pin_open_alerts", True) else {}
    )
    configured = core_and_extra(cfg)
    targets = [u.upper() for u in underlyings] if underlyings else list(configured)
    pin_only = [] if underlyings else [u for u in pins_by_und if u not in targets]
    result = RunResult(slot=st.name, date=st.date, dry_run=dry_run)

    todo = [
        u
        for u in [*targets, *pin_only]
        if force or dry_run or not snapshot_exists(root, u, st.date, st.name, st.hhmm)
    ]
    for u in [*targets, *pin_only]:
        if u not in todo:
            result.results.append(UResult(u, "skipped_exists"))
    if not todo:
        return result

    market_doc, rate, divs, mwarn = collect_market(provider, cfg, todo)
    result.warnings.extend(mwarn)
    defaulted = any(
        v.get("defaulted") for v in [market_doc["rate"], *market_doc["dividend_yield"].values()]
    )
    comp = str(cfg["storage"]["compression"])
    level = int(cfg["storage"]["compression_level"])
    filt = cfg["snapshot"]["filters"]
    dte_min, dte_max = int(filt["dte_min"]), int(filt["dte_max"])

    for u in todo:
        pins = pins_by_und.get(u, set())
        is_pin_only = u in pin_only
        ur = UResult(u, "failed")
        try:
            exps = provider.expirations(u)
            keep: list[date] = []
            pin_exp = {p[0] for p in pins}
            for e in exps:
                dte = (e - st.date).days
                if (not is_pin_only and dte_min <= dte <= dte_max) or e.isoformat() in pin_exp:
                    keep.append(e)
            if not keep:
                raise ProviderError(f"{u}: no expiries selected (provider listed {len(exps)})")
            raw: RawChain = provider.fetch_chain(u, keep)
            use_filters = {**filt, "dte_min": 1, "dte_max": 0} if is_pin_only else filt
            df = build_chain(
                raw,
                slot=st.name,
                rate=rate,
                div_yield=divs[u],
                filters=use_filters,
                pinned=pins,
                quality_cfg=cfg["quality"],
            )
            mo = market_is_open(raw.fetched_at)
            min_rows = 1 if is_pin_only else int(cfg["snapshot"]["broken_chain"]["min_rows"])
            bad = check_chain(df, cfg, mo, min_rows=min_rows)
            if bad:
                raise ProviderError(f"{u}: broken chain rejected: {bad}")
            summ = summarize(df)
            meta = {
                "underlying": u,
                "date": st.date.isoformat(),
                "slot": st.name,
                "hhmm": st.hhmm,
                "snapshot_ts_utc": fmt_ts(raw.fetched_at),
                "underlying_price": float(raw.underlying_price),
                "provider": raw.provider,
                "library_versions": _versions(provider),
                **summ,
                "pinned_rows": int(df["pinned"].sum()),
                "filters": use_filters,
                "expiries_fetched": len(keep),
                "greeks_inputs_defaulted": bool(
                    market_doc["rate"].get("defaulted")
                    or market_doc["dividend_yield"][u].get("defaulted")
                ),
                "r_used": rate,
                "q_used": divs[u],
                "r_source": market_doc["rate"]["source"],
                "q_source": market_doc["dividend_yield"][u]["source"],
                "market_open_at_fetch": bool(mo),
                "pin_only": is_pin_only,
                "unusable_flags": cfg["quality"]["unusable_flags"],
            }
            if dry_run:
                with tempfile.TemporaryDirectory() as td:
                    size = write_parquet(df, Path(td) / "x.parquet", comp, level)
                ur = UResult(
                    u,
                    "dry_run",
                    summ["rows"],
                    summ["usable_pct"],
                    size,
                    summ["flag_counts"],
                    meta["pinned_rows"],
                )
                ur.warnings.append(f"market_open_at_fetch={mo}")
            else:
                path = daily_path(root, u, st.date, st.hhmm)
                size = write_parquet(df, path, comp, level)
                meta["parquet_bytes"] = size
                atomic_write_bytes(meta_path(path), dumps(meta).encode())
                ur = UResult(
                    u,
                    "ok",
                    summ["rows"],
                    summ["usable_pct"],
                    size,
                    summ["flag_counts"],
                    meta["pinned_rows"],
                    path=str(path.relative_to(root)),
                )
        except Exception as exc:  # per-underlying isolation: one failure must not block the others
            ur = UResult(u, "failed", error=f"{type(exc).__name__}: {exc}")
            log.error("snapshot %s %s failed: %s", u, st.name, exc)
        result.results.append(ur)

    if dry_run:
        return result

    if any(r.status == "ok" for r in result.results):
        write_market_slot(root, st.date, st.name, market_doc, force)
    status = "ok" if result.ok else "failed"
    detail = "; ".join(f"{r.underlying}:{r.status}" for r in result.results)
    heartbeat.record(
        root, "snapshot", status=status, slot_key=f"{st.date.isoformat()}/{st.name}", detail=detail
    )

    if notify:
        _notify(root, cfg, result, defaulted)
    try:
        from odj.journal.summary import write_summary

        write_summary(root, now=now)
    except Exception as exc:  # the summary must never fail a snapshot
        log.warning("summary update failed: %s", exc)
    return result


def _notify(root: Path, cfg: dict[str, Any], result: RunResult, defaulted: bool) -> None:
    for r in result.results:
        if r.status == "failed":
            discord_post(
                f"snapshot {result.date} `{result.slot}` {r.underlying} FAILED: {r.error}", "fail"
            )
    warns = list(result.warnings)
    for r in result.results:
        if r.status == "ok" and r.usable_pct is not None and r.usable_pct < 50:
            warns.append(f"{r.underlying}: only {r.usable_pct:.0f}% of rows usable")
    if defaulted or warns:
        discord_post(
            f"snapshot {result.date} `{result.slot}` greeks inputs defaulted / data warnings:\n- "
            + "\n- ".join(warns or ["(inputs defaulted)"]),
            "warn",
        )
    from odj.snapshot.compact import size_report

    rep = size_report(root, cfg)
    if rep["over_limit"]:
        discord_post(
            f"repo data size {rep['total_mb']:.0f} MB exceeds {rep['limit_mb']} MB "
            f"(data/ {rep['data_mb']:.0f} MB, .git {rep['git_mb']:.0f} MB). Consider archiving old months.",
            "warn",
        )
