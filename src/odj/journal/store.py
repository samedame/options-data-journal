"""Append-only monthly JSONL journal with schema validation, idempotency and referential checks.

Layout: journal/{alerts,fills,exits,skips}/YYYY-MM.jsonl (month of the record's ts_utc).
Writes take an exclusive file lock per kind, so concurrent writers (threads or processes) cannot
interleave or lose lines. Git-level concurrency is handled by the union merge driver
(.gitattributes) plus `odj ci publish`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from odj.common.io import file_lock, read_jsonl, rewrite_jsonl
from odj.common.schema import KINDS, validate
from odj.common.timeutil import month_key

DIRS = {"alert": "alerts", "fill": "fills", "exit": "exits", "skip": "skips"}


class JournalError(ValueError):
    pass


class ConflictError(JournalError):
    """Same id already stored with different content."""


class ReferenceError_(JournalError):
    """A referenced alert/fill does not exist (or an exit over-closes a fill)."""


def kind_dir(root: Path, kind: str) -> Path:
    return root / "journal" / DIRS[kind]


def month_file(root: Path, kind: str, ts_utc: str) -> Path:
    return kind_dir(root, kind) / f"{month_key(ts_utc)}.jsonl"


def files(root: Path, kind: str) -> list[Path]:
    d = kind_dir(root, kind)
    return sorted(d.glob("*.jsonl")) if d.exists() else []


def read_all(root: Path, kind: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for f in files(root, kind):
        rows.extend(read_jsonl(f))
    return sorted(rows, key=lambda r: (r.get("ts_utc", ""), r.get("id", "")))


def get(root: Path, kind: str, rec_id: str) -> dict[str, Any] | None:
    for r in read_all(root, kind):
        if r.get("id") == rec_id:
            return r
    return None


def _canon(rec: dict[str, Any]) -> str:
    return json.dumps(rec, sort_keys=True, ensure_ascii=False)


def _check_refs(root: Path, kind: str, rec: dict[str, Any]) -> None:
    if kind == "fill" and rec.get("alert_id") and get(root, "alert", rec["alert_id"]) is None:
        raise ReferenceError_(f"fill references unknown alert_id {rec['alert_id']!r}")
    if kind == "skip" and get(root, "alert", rec["alert_id"]) is None:
        raise ReferenceError_(f"skip references unknown alert_id {rec['alert_id']!r}")
    if kind == "exit":
        fill = get(root, "fill", rec["fill_id"])
        if fill is None:
            raise ReferenceError_(f"exit references unknown fill_id {rec['fill_id']!r}")
        closed = sum(
            e["contracts"] for e in read_all(root, "exit") if e["fill_id"] == rec["fill_id"]
        )
        if closed + rec["contracts"] > fill["contracts"]:
            raise ReferenceError_(
                f"exit closes {rec['contracts']} contracts but only "
                f"{fill['contracts'] - closed} remain open on {rec['fill_id']!r}"
            )


def append(
    root: Path,
    kind: str,
    rec: dict[str, Any],
    *,
    check_refs: bool = True,
) -> tuple[dict[str, Any], bool]:
    """Validate and append. Returns (stored_record, created).

    * invalid -> ValidationFailed (nothing written)
    * same id + identical content -> (existing, False): idempotent no-op
    * same id + different content -> ConflictError (nothing written)
    """
    if kind not in KINDS:
        raise JournalError(f"unknown kind {kind!r}")
    norm = validate(kind, rec, root)
    target = month_file(root, kind, norm["ts_utc"])
    with file_lock(kind_dir(root, kind) / "_append"):
        existing = next((r for r in read_all(root, kind) if r["id"] == norm["id"]), None)
        if existing is not None:
            if _canon(existing) == _canon(norm):
                return existing, False
            raise ConflictError(f"{kind} id {norm['id']!r} already exists with different content")
        if check_refs:
            _check_refs(root, kind, norm)
        target.parent.mkdir(parents=True, exist_ok=True)
        line = _canon(norm) + "\n"
        with open(target, "a", encoding="utf-8") as fh:
            fh.write(line)
            fh.flush()
    return norm, True


def remove_test_records(root: Path, *, dry_run: bool = False) -> dict[str, int]:
    """Delete only records tagged `test: true` (all kinds). Returns counts removed per kind."""
    removed: dict[str, int] = {}
    for kind in KINDS:
        n = 0
        with file_lock(kind_dir(root, kind) / "_append"):
            for f in files(root, kind):
                rows = read_jsonl(f)
                keep = [r for r in rows if r.get("test") is not True]
                n += len(rows) - len(keep)
                if len(keep) != len(rows) and not dry_run:
                    if keep:
                        rewrite_jsonl(f, keep)
                    else:
                        f.unlink()
        removed[kind] = n
    return removed
