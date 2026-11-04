"""Append-only journal: idempotency, conflicts, references, concurrency, test-data reset."""

from __future__ import annotations

import json
import multiprocessing as mp
from pathlib import Path

import pytest

from odj.common.schema import ValidationFailed
from odj.journal import store
from tests.helpers import alert, exit_, fill


def test_append_is_idempotent_and_conflicts_are_loud(root: Path) -> None:
    rec, created = store.append(root, "alert", alert())
    assert created and (root / "journal/alerts/2026-11.jsonl").exists()
    again, created2 = store.append(root, "alert", alert())
    assert not created2 and again == rec
    assert len((root / "journal/alerts/2026-11.jsonl").read_text().splitlines()) == 1
    with pytest.raises(store.ConflictError):
        store.append(root, "alert", alert(alert_net_price=1.11))
    assert len(store.read_all(root, "alert")) == 1


def test_invalid_record_writes_nothing(root: Path) -> None:
    with pytest.raises(ValidationFailed):
        store.append(root, "alert", alert(structure="nope"))
    assert not (root / "journal/alerts/2026-11.jsonl").exists()


def test_records_land_in_the_month_of_their_timestamp(root: Path) -> None:
    store.append(
        root, "alert", alert(ts_utc="2026-12-31T23:59:00Z", id="spy-bps-bot-20261231T235900Z-1")
    )
    store.append(root, "alert", alert())
    assert sorted(p.name for p in (root / "journal/alerts").glob("*.jsonl")) == [
        "2026-11.jsonl",
        "2026-12.jsonl",
    ]


def test_referential_integrity(root: Path) -> None:
    with pytest.raises(store.ReferenceError_):
        store.append(root, "fill", fill())  # alert unknown
    store.append(root, "alert", alert())
    store.append(root, "fill", fill())
    with pytest.raises(store.ReferenceError_):
        store.append(root, "exit", exit_(fill_id="nope"))
    store.append(root, "exit", exit_(contracts=1, id="exit-1"))
    with pytest.raises(store.ReferenceError_, match="only 1 remain"):
        store.append(root, "exit", exit_(contracts=2, id="exit-2"))
    with pytest.raises(store.ReferenceError_):
        store.append(
            root,
            "skip",
            {
                "id": "skip-1",
                "alert_id": "ghost-20261102T000000Z-1",
                "ts_utc": "2026-11-02T15:00:00Z",
                "reason": "other",
            },
        )


def _worker(args: tuple[str, int, int]) -> int:
    root, worker, n = args
    ok = 0
    for i in range(n):
        _, created = store.append(
            Path(root),
            "alert",
            alert(
                id=f"spy-bps-bot-20261102T1435{worker:02d}Z-{i}",
                ts_utc=f"2026-11-02T14:35:{worker:02d}Z",
            ),
            check_refs=False,
        )
        ok += created
    return ok


def test_concurrent_ingests_lose_nothing(root: Path) -> None:
    ctx = mp.get_context("fork")
    with ctx.Pool(4) as pool:
        counts = pool.map(_worker, [(str(root), w, 25) for w in range(4)])
    assert sum(counts) == 100
    lines = (root / "journal/alerts/2026-11.jsonl").read_text().splitlines()
    assert len(lines) == 100 and all(
        json.loads(x)["bot"] == "spy-bps-bot" for x in lines
    )  # no torn/interleaved lines
    assert len({json.loads(x)["id"] for x in lines}) == 100


def _same_id_worker(root: str) -> bool:
    return store.append(Path(root), "alert", alert())[1]


def test_simultaneous_identical_alert_is_stored_once(root: Path) -> None:
    with mp.get_context("fork").Pool(4) as pool:
        created = pool.map(_same_id_worker, [str(root)] * 8)
    assert sum(created) == 1
    assert len(store.read_all(root, "alert")) == 1


def test_remove_test_records_touches_only_tagged(root: Path) -> None:
    store.append(root, "alert", alert(test=True))
    store.append(
        root, "alert", alert(id="spy-bps-bot-20261102T150000Z-2", ts_utc="2026-11-02T15:00:00Z")
    )
    store.append(root, "fill", fill(alert_id=None, id="f-test", test=True))
    store.append(root, "fill", fill(alert_id=None, id="f-real"))
    assert store.remove_test_records(root, dry_run=True) == {
        "alert": 1,
        "fill": 1,
        "exit": 0,
        "skip": 0,
    }
    assert len(store.read_all(root, "alert")) == 2  # dry run removed nothing
    assert store.remove_test_records(root) == {"alert": 1, "fill": 1, "exit": 0, "skip": 0}
    assert [a["id"] for a in store.read_all(root, "alert")] == ["spy-bps-bot-20261102T150000Z-2"]
    assert [f["id"] for f in store.read_all(root, "fill")] == ["f-real"]
