"""Repo-root discovery and YAML config loading."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

ROOT_ENV = "ODJ_ROOT"


def find_root(start: Path | None = None) -> Path:
    """Return the repo root: $ODJ_ROOT, else the nearest parent holding pyproject.toml + config/."""
    env = os.environ.get(ROOT_ENV)
    if env:
        return Path(env).resolve()
    here = (start or Path.cwd()).resolve()
    for p in [here, *here.parents]:
        if (p / "config" / "tickers.yml").exists():
            return p
    # fall back to the package location (editable install)
    pkg_root = Path(__file__).resolve().parents[3]
    if (pkg_root / "config" / "tickers.yml").exists():
        return pkg_root
    raise FileNotFoundError("Cannot find repo root (set ODJ_ROOT or run inside the repo).")


@lru_cache(maxsize=16)
def _load_yaml(path: str, mtime: float) -> dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path}: top-level YAML must be a mapping")
    return data


def load_yaml(path: Path) -> dict[str, Any]:
    return _load_yaml(str(path), path.stat().st_mtime)


def tickers_cfg(root: Path | None = None) -> dict[str, Any]:
    return load_yaml((root or find_root()) / "config" / "tickers.yml")


def journal_cfg(root: Path | None = None) -> dict[str, Any]:
    return load_yaml((root or find_root()) / "config" / "journal.yml")


def core_and_extra(cfg: dict[str, Any]) -> list[str]:
    u = cfg.get("underlyings", {})
    out: list[str] = []
    for sym in [*u.get("core", []), *u.get("extra", [])]:
        s = str(sym).upper()
        if s not in out:
            out.append(s)
    return out
