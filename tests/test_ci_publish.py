"""Git-level concurrency: several runs commit + push at once; union merge + fetch/rebase/push retries."""

from __future__ import annotations

import json
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from odj.ci import publish
from odj.common import heartbeat

ENV = {"ODJ_GIT_NAME": "ci-bot", "ODJ_GIT_EMAIL": "ci@example.invalid"}


def git(cwd: Path, *args: str) -> str:
    cp = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return cp.stdout


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    bare = tmp_path / "origin.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(bare)], check=True, capture_output=True
    )
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-b", "main")
    (seed / ".gitattributes").write_text("journal/**/*.jsonl merge=union\n")
    (seed / "journal/alerts").mkdir(parents=True)
    (seed / "journal/alerts/2026-11.jsonl").write_text(json.dumps({"id": "seed"}) + "\n")
    (seed / "status").mkdir()
    (seed / "status/heartbeat.json").write_text('{"jobs": {}}\n')
    git(seed, "add", "-A")
    git(seed, "commit", "-m", "seed")
    git(seed, "remote", "add", "origin", str(bare))
    git(seed, "push", "origin", "main")
    return bare


def clone(origin: Path, tmp_path: Path, name: str, monkeypatch: pytest.MonkeyPatch) -> Path:
    dest = tmp_path / name
    subprocess.run(["git", "clone", str(origin), str(dest)], check=True, capture_output=True)
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    return dest


def add_alert(repo: Path, rec_id: str) -> None:
    with open(repo / "journal/alerts/2026-11.jsonl", "a") as fh:
        fh.write(json.dumps({"id": rec_id}) + "\n")
    heartbeat.record(repo, f"job-{rec_id}", status="ok")


def origin_ids(origin: Path, tmp_path: Path) -> list[str]:
    check = tmp_path / "check"
    subprocess.run(["git", "clone", str(origin), str(check)], check=True, capture_output=True)
    return [
        json.loads(x)["id"]
        for x in (check / "journal/alerts/2026-11.jsonl").read_text().splitlines()
    ]


def test_stale_clone_rebases_and_both_alerts_survive(
    origin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a, b = clone(origin, tmp_path, "a", monkeypatch), clone(origin, tmp_path, "b", monkeypatch)
    add_alert(a, "alert-a")
    add_alert(b, "alert-b")  # b's clone is now stale w.r.t. origin once a pushes
    assert publish(a, "a", sleep=lambda s: None)["status"] == "pushed"
    regen_calls: list[int] = []
    res = publish(b, "b", regen=lambda: regen_calls.append(1), sleep=lambda s: None)
    assert res["status"] == "pushed"
    assert regen_calls  # derived files regenerated after the rebase
    assert sorted(origin_ids(origin, tmp_path)) == ["alert-a", "alert-b", "seed"]
    hb = json.loads((tmp_path / "check/status/heartbeat.json").read_text())["jobs"]
    assert "job-alert-a" in hb and "job-alert-b" in hb  # both heartbeat writes merged


def test_many_simultaneous_publishers_lose_nothing(
    origin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repos = [clone(origin, tmp_path, f"c{i}", monkeypatch) for i in range(4)]
    for i, r in enumerate(repos):
        add_alert(r, f"alert-{i}")
    with ThreadPoolExecutor(4) as ex:
        results = list(
            ex.map(lambda r: publish(r, f"msg {r.name}", attempts=10, sleep=lambda s: None), repos)
        )
    assert all(r["status"] == "pushed" for r in results)
    ids = origin_ids(origin, tmp_path)
    assert sorted(ids) == sorted(["seed", *(f"alert-{i}" for i in range(4))])
    assert len(ids) == len(set(ids))


def test_nothing_to_commit_is_a_clean_noop(
    origin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = clone(origin, tmp_path, "a", monkeypatch)
    assert publish(a, "nothing", sleep=lambda s: None)["status"] == "nothing-to-commit"


def test_gives_up_after_bounded_attempts(
    origin: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = clone(origin, tmp_path, "a", monkeypatch)
    add_alert(a, "alert-a")
    git(a, "remote", "set-url", "origin", str(tmp_path / "does-not-exist.git"))
    from odj.ci import PublishError

    with pytest.raises(PublishError):
        publish(a, "will fail", attempts=2, sleep=lambda s: None)
