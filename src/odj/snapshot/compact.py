"""Monthly compaction + repo-size guard.

NOTE (see DECISIONS.md): compaction merges many small daily files into one parquet per
underlying-month, which speeds up loading and shrinks the working tree's file count. It does NOT
shrink `.git` (history keeps the daily blobs), so the size guard measures `data/` AND `.git`.
"""

from __future__ import annotations

import os
import shutil
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

from odj.common.config import tickers_cfg
from odj.common.io import atomic_write_bytes, dumps, read_json
from odj.common.timeutil import ET, now_utc
from odj.snapshot.store import (
    chains_dir,
    compacted_dir,
    daily_path,
    meta_path,
    snapshot_index,
    write_parquet,
)


def dir_size_bytes(p: Path) -> int:
    total = 0
    if not p.exists():
        return 0
    for dirpath, _dirs, files in os.walk(p):
        for f in files:
            try:
                total += (Path(dirpath) / f).stat().st_size
            except OSError:
                pass
    return total


def size_report(root: Path, cfg: dict[str, Any] | None = None) -> dict[str, Any]:
    """Repo-size estimate for the guard.

    * data_mb: working-tree size of data/.
    * git_mb: size of a local .git (a shallow CI checkout under-reports history).
    * compacted_mb: bytes held in compacted/ files; compaction deletes the daily files from the working
      tree but git HISTORY keeps their blobs, so the server-side repo is roughly data/ + compacted/.
    The guard uses the larger of (data + compacted) and (data + .git).
    """
    cfg = cfg or tickers_cfg(root)
    limit = int(cfg["storage"]["size_warn_mb"])
    data_mb = dir_size_bytes(root / "data") / 1e6
    git_mb = dir_size_bytes(root / ".git") / 1e6
    compacted = (
        sum(
            f.stat().st_size
            for f in (root / "data" / "chains").glob("*/compacted/*.parquet")
            if f.is_file()
        )
        / 1e6
    )
    estimated = data_mb + compacted
    total = max(estimated, data_mb + git_mb)
    return {
        "data_mb": round(data_mb, 2),
        "git_mb": round(git_mb, 2),
        "compacted_mb": round(compacted, 2),
        "estimated_repo_mb": round(estimated, 2),
        "total_mb": round(total, 2),
        "limit_mb": limit,
        "over_limit": total > limit,
    }


def compact_month(
    root: Path,
    underlying: str,
    month: str,
    *,
    keep_daily: bool = False,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Merge all daily snapshots of `underlying` in `month` ('YYYY-MM') into compacted/{month}.parquet.

    Idempotent: with no daily files left it is a no-op. Row counts are verified before any daily
    file is removed."""
    cfg = cfg or tickers_cfg(root)
    comp = str(cfg["storage"]["compression"])
    level = int(cfg["storage"]["compression_level"])
    daily = [
        e
        for e in snapshot_index(root, underlying)
        if not e.compacted and e.date.strftime("%Y-%m") == month
    ]
    if not daily:
        return {"underlying": underlying, "month": month, "compacted": 0, "status": "nothing to do"}
    cdir = compacted_dir(root, underlying)
    out_pq = cdir / f"{month}.parquet"
    manifest_p = cdir / f"{month}.manifest.json"
    frames: list[pd.DataFrame] = []
    entries: list[dict[str, Any]] = []
    if out_pq.exists() and manifest_p.exists():  # merge into an existing compacted month
        frames.append(pd.read_parquet(out_pq))
        entries.extend((read_json(manifest_p, default={}) or {}).get("snapshots", []))
    have = {(e["date"], e["slot"]) for e in entries}
    expected_rows = sum(len(f) for f in frames)
    new_daily = []
    for e in daily:
        if (e.date.isoformat(), e.slot) in have:
            new_daily.append(e)  # already inside the compacted file; just clean up
            continue
        df = pd.read_parquet(e.path)
        frames.append(df)
        expected_rows += len(df)
        entries.append(
            {
                "date": e.date.isoformat(),
                "slot": e.slot,
                "hhmm": e.hhmm,
                "rows": len(df),
                "meta": e.meta,
            }
        )
        new_daily.append(e)
    merged = pd.concat(frames, ignore_index=True).sort_values(
        ["snapshot_ts_utc", "expiry", "right", "strike"], kind="stable"
    )
    merged = merged.drop_duplicates(subset=["snapshot_ts_utc", "slot", "expiry", "strike", "right"])
    cdir.mkdir(parents=True, exist_ok=True)
    stage = out_pq.with_name(out_pq.name + ".stage")
    write_parquet(merged, stage, comp, level)
    check = pd.read_parquet(stage)
    if len(check) != len(merged) or len(merged) != expected_rows:
        stage.unlink()
        raise RuntimeError(
            f"compaction verification failed for {underlying} {month}: row counts differ "
            f"(merged {len(merged)}, read back {len(check)}, expected {expected_rows})"
        )
    os.replace(stage, out_pq)
    entries.sort(key=lambda s: (s["date"], s["hhmm"]))
    atomic_write_bytes(
        manifest_p, dumps({"month": month, "underlying": underlying, "snapshots": entries}).encode()
    )
    removed = 0
    if not keep_daily:
        for e in new_daily:
            e.path.unlink(missing_ok=True)
            meta_path(e.path).unlink(missing_ok=True)
            removed += 1
            try:
                e.path.parent.rmdir()
            except OSError:
                pass
    return {
        "underlying": underlying,
        "month": month,
        "compacted": len(daily),
        "rows": len(merged),
        "bytes": out_pq.stat().st_size,
        "daily_removed": removed,
        "status": "ok",
    }


def compact_all(
    root: Path, *, month: str | None = None, keep_daily: bool = False, today: date | None = None
) -> list[dict[str, Any]]:
    """Compact every completed month (before the current New York month) for every underlying, or just `month`."""
    cfg = tickers_cfg(root)
    today = today or now_utc().astimezone(ET).date()
    cur = today.strftime("%Y-%m")
    results: list[dict[str, Any]] = []
    cdir = chains_dir(root)
    unds = sorted(p.name for p in cdir.iterdir() if p.is_dir()) if cdir.exists() else []
    for u in unds:
        months = {e.date.strftime("%Y-%m") for e in snapshot_index(root, u) if not e.compacted}
        for m in sorted(months):
            if month is not None and m != month:
                continue
            if month is None and m >= cur:
                continue
            results.append(compact_month(root, u, m, keep_daily=keep_daily, cfg=cfg))
    return results


def prune_empty_dirs(root: Path) -> None:
    cdir = chains_dir(root)
    if not cdir.exists():
        return
    for d in sorted(cdir.glob("*/*"), reverse=True):
        if d.is_dir() and not any(d.iterdir()):
            shutil.rmtree(d, ignore_errors=True)


__all__ = ["compact_all", "compact_month", "daily_path", "dir_size_bytes", "size_report"]
