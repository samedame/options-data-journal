"""Read/write API for the chain dataset.

Backtest usage::

    from odj.snapshot.store import load_chains, load_market

    chains = load_chains("SPY", "2026-11-01", "2026-12-31", dte_min=20, dte_max=45, right="P")
    # one row per option per snapshot; includes bid/ask/mid, iv_calc, delta, flags, `usable`
    # and `snapshot_date` (the New York trading date of the snapshot).
    mkt = load_market("2026-11-01", "2026-12-31")   # one row per (date, slot): vix, vix9d, vix3m, vxn, rate

``load_chains(..., usable_only=True)`` (default) drops rows carrying any flag listed in
``config/tickers.yml: quality.unusable_flags`` (zero_bid, zero_ask, crossed_market, wide_spread);
pass ``usable_only=False`` to see everything and filter on the flag columns yourself.

Layout::

    data/chains/{UNDERLYING}/{YYYY-MM-DD}/{HHMM}.parquet     + {HHMM}.meta.json   (daily)
    data/chains/{UNDERLYING}/compacted/{YYYY-MM}.parquet     + {YYYY-MM}.manifest.json
    data/market/{YYYY-MM-DD}.json                            (VIX family, rate, dividend yields)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from odj.common.config import find_root
from odj.common.io import read_json
from odj.common.timeutil import ET
from odj.snapshot.columns import CHAIN_COLUMNS

DateLike = date | str


def _d(x: DateLike) -> date:
    return x if isinstance(x, date) else date.fromisoformat(str(x))


def chains_dir(root: Path) -> Path:
    return root / "data" / "chains"


def daily_path(root: Path, underlying: str, d: date, hhmm: str) -> Path:
    return chains_dir(root) / underlying / d.isoformat() / f"{hhmm}.parquet"


def meta_path(parquet: Path) -> Path:
    return parquet.with_suffix(".meta.json")


def compacted_dir(root: Path, underlying: str) -> Path:
    return chains_dir(root) / underlying / "compacted"


def market_path(root: Path, d: date) -> Path:
    return root / "data" / "market" / f"{d.isoformat()}.json"


@dataclass(frozen=True)
class SnapEntry:
    underlying: str
    date: date
    slot: str
    hhmm: str
    path: Path
    compacted: bool
    meta: dict[str, Any]


def snapshot_index(root: Path, underlying: str) -> list[SnapEntry]:
    """Every stored snapshot (daily files with a sidecar + compacted manifests) for an underlying."""
    out: list[SnapEntry] = []
    udir = chains_dir(root) / underlying
    if not udir.exists():
        return out
    for ddir in sorted(p for p in udir.iterdir() if p.is_dir() and p.name != "compacted"):
        try:
            d = date.fromisoformat(ddir.name)
        except ValueError:
            continue
        for pq in sorted(ddir.glob("*.parquet")):
            meta = read_json(meta_path(pq), default=None)
            if not isinstance(meta, dict):
                continue  # parquet without sidecar = interrupted write; treated as absent
            out.append(
                SnapEntry(underlying, d, str(meta.get("slot", "")), pq.stem, pq, False, meta)
            )
    cdir = compacted_dir(root, underlying)
    if cdir.exists():
        for man in sorted(cdir.glob("*.manifest.json")):
            m = read_json(man, default={}) or {}
            pq = man.with_name(man.name.replace(".manifest.json", ".parquet"))
            for s in m.get("snapshots", []):
                out.append(
                    SnapEntry(
                        underlying,
                        date.fromisoformat(s["date"]),
                        str(s["slot"]),
                        str(s["hhmm"]),
                        pq,
                        True,
                        s.get("meta", {}),
                    )
                )
    return sorted(out, key=lambda e: (e.date, e.hhmm))


def snapshot_exists(root: Path, underlying: str, d: date, slot: str, hhmm: str) -> bool:
    """Idempotency key = (underlying, date, slot)."""
    p = daily_path(root, underlying, d, hhmm)
    if p.exists() and meta_path(p).exists():
        return True
    return any(
        e.date == d and e.slot == slot for e in snapshot_index(root, underlying) if e.compacted
    )


def write_parquet(df: pd.DataFrame, path: Path, compression: str = "zstd", level: int = 9) -> int:
    """Atomically write a zstd parquet file; returns its size in bytes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    kwargs: dict[str, Any] = {"compression": compression, "compression_level": level}
    df.to_parquet(tmp, engine="pyarrow", index=False, **kwargs)
    os.replace(tmp, path)
    return path.stat().st_size


def _empty() -> pd.DataFrame:
    df = pd.DataFrame({c: pd.Series(dtype="object") for c in [*CHAIN_COLUMNS, "snapshot_date"]})
    return df


def load_chains(
    underlying: str,
    start: DateLike,
    end: DateLike,
    dte_min: int | None = None,
    dte_max: int | None = None,
    right: str | None = None,
    usable_only: bool = True,
    *,
    root: Path | None = None,
    columns: list[str] | None = None,
) -> pd.DataFrame:
    """Load stored chain snapshots for `underlying` with snapshot_date in [start, end] (inclusive)."""
    root = root or find_root()
    s, e = _d(start), _d(end)
    filters: list[tuple[str, str, Any]] = []
    if dte_min is not None:
        filters.append(("dte", ">=", int(dte_min)))
    if dte_max is not None:
        filters.append(("dte", "<=", int(dte_max)))
    if right is not None:
        filters.append(("right", "==", right.upper()[:1]))
    if usable_only:
        filters.append(("usable", "==", True))
    frames: list[pd.DataFrame] = []
    seen_compacted: set[Path] = set()
    for ent in snapshot_index(root, underlying):
        if not (s <= ent.date <= e):
            continue
        if ent.compacted:
            if ent.path in seen_compacted:
                continue
            seen_compacted.add(ent.path)
            df = pd.read_parquet(ent.path, columns=columns, filters=filters or None)
        else:
            df = pd.read_parquet(ent.path, columns=columns, filters=filters or None)
        if df.empty:
            continue
        frames.append(df)
    if not frames:
        return _empty()
    out = pd.concat(frames, ignore_index=True)
    if "snapshot_ts_utc" in out:
        out["snapshot_date"] = out["snapshot_ts_utc"].dt.tz_convert(ET).dt.date
        out = out[(out["snapshot_date"] >= s) & (out["snapshot_date"] <= e)]
        # compacted months can hold other days than requested; dedupe (date, slot, contract) defensively
        keys = [c for c in ("snapshot_ts_utc", "slot", "expiry", "strike", "right") if c in out]
        out = out.drop_duplicates(subset=keys)
        out = out.sort_values(
            [c for c in ("snapshot_ts_utc", "expiry", "right", "strike") if c in out]
        )
    return out.reset_index(drop=True)


def load_quotes(
    root: Path,
    underlying: str,
    keys: set[tuple[str, float, str]],
    start: date,
    end: date,
) -> pd.DataFrame:
    """Only the rows for specific contracts (expiry 'YYYY-MM-DD', strike, 'C'/'P'): used by the shadow
    tracker so it never loads whole chains. Includes unusable rows (flags kept) - callers judge quotes."""
    if not keys:
        return _empty()
    expiries = sorted({k[0] for k in keys})
    cols = [
        "snapshot_ts_utc",
        "slot",
        "underlying_price",
        "expiry",
        "dte",
        "strike",
        "right",
        "bid",
        "ask",
        "mid",
    ]
    exp_ts = [pd.Timestamp(e) for e in expiries]
    frames: list[pd.DataFrame] = []
    done: set[Path] = set()
    for ent in snapshot_index(root, underlying):
        if not (start <= ent.date <= end):
            continue
        if ent.compacted:
            if ent.path in done:
                continue
            done.add(ent.path)
        df = pd.read_parquet(ent.path, columns=cols, filters=[("expiry", "in", exp_ts)])
        if not df.empty:
            frames.append(df)
    if not frames:
        return _empty()
    out = pd.concat(frames, ignore_index=True)
    out["_k"] = [
        (e.strftime("%Y-%m-%d"), round(float(k), 4), r)
        for e, k, r in zip(out["expiry"], out["strike"], out["right"], strict=True)
    ]
    out = out[out["_k"].isin(keys)].drop(columns="_k")
    return out.drop_duplicates(
        subset=["snapshot_ts_utc", "slot", "expiry", "strike", "right"]
    ).reset_index(drop=True)


def load_market(start: DateLike, end: DateLike, *, root: Path | None = None) -> pd.DataFrame:
    """One row per (date, slot): vix, vix9d, vix3m, vxn, rate (fraction), rate_defaulted, div_yield_<SYM>."""
    root = root or find_root()
    s, e = _d(start), _d(end)
    rows: list[dict[str, Any]] = []
    mdir = root / "data" / "market"
    for p in sorted(mdir.glob("*.json")) if mdir.exists() else []:
        try:
            d = date.fromisoformat(p.stem)
        except ValueError:
            continue
        if not (s <= d <= e):
            continue
        doc = read_json(p, default={}) or {}
        for slot, sd in (doc.get("slots") or {}).items():
            row: dict[str, Any] = {"date": d, "slot": slot}
            for k in ("vix", "vix9d", "vix3m", "vxn", "rate"):
                row[k] = (sd.get(k) or {}).get("value")
            row["rate_defaulted"] = bool((sd.get("rate") or {}).get("defaulted", False))
            for sym, dv in (sd.get("dividend_yield") or {}).items():
                row[f"div_yield_{sym}"] = (dv or {}).get("value")
            rows.append(row)
    return pd.DataFrame(rows)
