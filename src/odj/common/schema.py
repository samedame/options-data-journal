"""JSON-Schema loading, record normalization and validation."""

from __future__ import annotations

import copy
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from odj.common.config import find_root
from odj.common.timeutil import normalize_ts

KINDS = ("alert", "fill", "exit", "skip")


class ValidationFailed(ValueError):
    def __init__(self, kind: str, errors: list[str]):
        self.kind = kind
        self.errors = errors
        super().__init__(f"invalid {kind}: " + "; ".join(errors))


@lru_cache(maxsize=16)
def _validator(kind: str, root_s: str) -> Draft202012Validator:
    p = Path(root_s) / "schemas" / f"{kind}.schema.json"
    with open(p, encoding="utf-8") as fh:
        schema = json.load(fh)
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema)


def validator(kind: str, root: Path | None = None) -> Draft202012Validator:
    return _validator(kind, str((root or find_root()).resolve()))


def _legs_of(rec: dict[str, Any], key: str) -> list[dict[str, Any]]:
    legs = rec.get(key)
    return [leg for leg in legs if isinstance(leg, dict)] if isinstance(legs, list) else []


def normalize(kind: str, rec: dict[str, Any]) -> dict[str, Any]:
    """Return a copy with timestamps normalized to UTC-Z and defaults filled in. Does not validate."""
    out = copy.deepcopy(rec)
    if isinstance(out.get("ts_utc"), str):
        try:
            out["ts_utc"] = normalize_ts(out["ts_utc"])
        except ValueError:
            pass  # validation will report it
    if kind == "alert":
        out.setdefault("exit_rules", {})
        for leg in _legs_of(out, "legs"):
            leg.setdefault("ratio", 1)
    if kind == "fill":
        out.setdefault("broker", "robinhood")
        out.setdefault("fees_total", 0.0)
        out.setdefault("ts_precision", "second")
        for leg in _legs_of(out, "legs_actual"):
            leg.setdefault("ratio", 1)
    if kind == "exit":
        out.setdefault("fees_total", 0.0)
        out.setdefault("ts_precision", "second")
    return out


def _semantic_errors(kind: str, rec: dict[str, Any]) -> list[str]:
    errs: list[str] = []
    if kind == "alert":
        has_opt = any(leg.get("type") in ("call", "put") for leg in _legs_of(rec, "legs"))
        if rec.get("structure") not in ("equity_long", "equity_short") and not has_opt:
            errs.append("structure requires at least one option leg")
        rid, bot = rec.get("id"), rec.get("bot")
        if isinstance(rid, str) and isinstance(bot, str) and not rid.startswith(bot + "-"):
            errs.append(f"id must start with '{bot}-'")
    return errs


def validate(kind: str, rec: dict[str, Any], root: Path | None = None) -> dict[str, Any]:
    """Normalize + validate. Returns the normalized record or raises ValidationFailed."""
    if kind not in KINDS:
        raise ValueError(f"unknown kind {kind!r}")
    norm = normalize(kind, rec)
    errs = [
        f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
        for e in sorted(
            validator(kind, root).iter_errors(norm), key=lambda e: list(e.absolute_path)
        )
    ]
    if not errs:
        errs = _semantic_errors(kind, norm)
    if errs:
        raise ValidationFailed(kind, errs[:12])
    return norm
