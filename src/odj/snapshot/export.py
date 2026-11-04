"""`odj export`: csv or parquet. No OptionsDX exporter: no sample OptionsDX files were found locally
(see DECISIONS.md), so there is no verified column layout to match."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

from odj.common.config import find_root
from odj.snapshot.store import load_chains


def export_chains(
    underlying: str,
    start: date | str,
    end: date | str,
    fmt: str,
    out: Path | None = None,
    *,
    root: Path | None = None,
    usable_only: bool = True,
    dte_min: int | None = None,
    dte_max: int | None = None,
) -> tuple[Path, int]:
    root = root or find_root()
    if fmt not in ("csv", "parquet"):
        raise ValueError(
            "format must be csv or parquet (no optionsdx exporter: no sample file was found)"
        )
    df = load_chains(
        underlying, start, end, dte_min=dte_min, dte_max=dte_max, usable_only=usable_only, root=root
    )
    out = out or root / "exports" / f"{underlying}_{start}_{end}.{fmt}"
    out.parent.mkdir(parents=True, exist_ok=True)
    if fmt == "csv":
        _csv_ready(df).to_csv(out, index=False)
    else:
        df.to_parquet(out, engine="pyarrow", compression="zstd", index=False)
    return out, len(df)


def _csv_ready(df: pd.DataFrame) -> pd.DataFrame:
    d = df.copy()
    if d.empty:
        return d
    for c in ("snapshot_ts_utc", "last_trade_ts"):
        if c in d:
            d[c] = d[c].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    if "expiry" in d:
        d["expiry"] = d["expiry"].dt.strftime("%Y-%m-%d")
    return d
