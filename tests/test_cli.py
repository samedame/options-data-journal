"""CLI flows: the three ways of logging, dry-runs, test-data reset, summary, report."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from odj.cli import main
from odj.journal import store
from tests.helpers import alert

LEGS = "sell put 530 2026-11-20; buy put 525 2026-11-20"


def run(*argv: str) -> int:
    return main(list(argv))


def test_alert_fill_exit_skip_via_cli_with_test_tag_and_reset(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        run(
            "alert",
            "add",
            "--id",
            "cli-bot-20261102T143500Z-1",
            "--ts",
            "2026-11-02T14:35:00Z",
            "--bot",
            "cli-bot",
            "--strategy",
            "S1",
            "--underlying",
            "spy",
            "--structure",
            "put_credit_spread",
            "--legs",
            LEGS,
            "--net",
            "0.9",
            "--max-loss",
            "410",
            "--profit-target",
            "50",
            "--stop",
            "2",
            "--test",
        )
        == 0
    )
    assert (
        run(
            "fill",
            "add",
            "--id",
            "fill-cli-1",
            "--alert-id",
            "cli-bot-20261102T143500Z-1",
            "--ts",
            "2026-11-02T14:40:00Z",
            "--contracts",
            "2",
            "--net",
            "0.85",
            "--fees",
            "1.3",
            "--test",
        )
        == 0
    )
    assert "entry slippage vs alert" in capsys.readouterr().out
    assert (
        run(
            "exit",
            "add",
            "--id",
            "exit-cli-1",
            "--fill-id",
            "fill-cli-1",
            "--ts",
            "2026-11-10T15:00:00Z",
            "--contracts",
            "2",
            "--net",
            "-0.2",
            "--reason",
            "profit_target",
            "--test",
        )
        == 0
    )
    assert (
        run(
            "skip",
            "add",
            "--id",
            "skip-cli-1",
            "--alert-id",
            "cli-bot-20261102T143500Z-1",
            "--reason",
            "capital",
            "--test",
        )
        == 0
    )
    assert [len(store.read_all(root, k)) for k in ("alert", "fill", "exit", "skip")] == [1, 1, 1, 1]
    # a real (untagged) record must survive the reset
    store.append(root, "alert", alert())
    assert run("reset-test-data", "--dry-run") == 0
    assert len(store.read_all(root, "alert")) == 2
    assert run("reset-test-data", "--yes") == 0
    assert [a["id"] for a in store.read_all(root, "alert")] == [alert()["id"]]
    assert [len(store.read_all(root, k)) for k in ("fill", "exit", "skip")] == [0, 0, 0]
    doc = json.loads((root / "data/summary/latest.json").read_text())
    assert doc["schema_version"] == 1 and doc["bots"]["spy-bps-bot"]["alerts"] == 1


def test_dry_run_validates_and_writes_nothing(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = run(
        "alert",
        "add",
        "--dry-run",
        "--bot",
        "cli-bot",
        "--strategy",
        "S1",
        "--underlying",
        "SPY",
        "--structure",
        "put_credit_spread",
        "--legs",
        LEGS,
        "--net",
        "0.9",
    )
    assert rc == 0 and "dry-run: valid alert" in capsys.readouterr().out
    assert not (root / "journal/alerts").exists() or not list(
        (root / "journal/alerts").glob("*.jsonl")
    )


def test_invalid_cli_input_is_rejected_without_writing(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = run(
        "alert",
        "add",
        "--bot",
        "cli-bot",
        "--strategy",
        "S1",
        "--underlying",
        "SPY",
        "--structure",
        "nonsense",
        "--legs",
        LEGS,
        "--net",
        "0.9",
    )
    assert rc == 2 and "rejected, nothing written" in capsys.readouterr().err
    assert (
        run(
            "fill",
            "add",
            "--alert-id",
            "ghost-20260101T000000Z-1",
            "--contracts",
            "1",
            "--net",
            "0.5",
        )
        == 2
    )
    assert (
        run(
            "alert",
            "add",
            "--bot",
            "cli-bot",
            "--strategy",
            "S",
            "--underlying",
            "SPY",
            "--structure",
            "put_credit_spread",
            "--legs",
            "bogus",
            "--net",
            "1",
        )
        == 2
    )
    assert store.read_all(root, "alert") == []


def test_alert_validate_and_ingest_stdin(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    f = tmp_path / "a.json"
    f.write_text(json.dumps(alert()))
    assert run("alert", "validate", "--json", str(f)) == 0 and "valid" in capsys.readouterr().out
    f.write_text(json.dumps(alert(structure="x")))
    assert run("alert", "validate", "--json", str(f)) == 2
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(alert())))
    assert run("ingest-alert", "--no-notify", "--dry-run") == 0
    assert store.read_all(root, "alert") == []  # dry run


def test_ingest_issue_command_writes_comment_file_and_outputs(
    root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store.append(root, "alert", alert())
    out = tmp_path / "gh_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    body = (
        "### Alert ID\n\n"
        + alert()["id"]
        + "\n\n### Contracts\n\n2\n\n### Net price per share\n\n0.85\n\n### Order type\n\nlimit\n"
    )
    ev = {
        "issue": {
            "number": 7,
            "user": {"login": "samedame"},
            "labels": [],
            "body": body,
            "created_at": "2026-11-02T14:40:00Z",
            "title": "[fill] x",
        },
        "repository": {"owner": {"login": "samedame"}},
    }
    evf = tmp_path / "ev.json"
    evf.write_text(json.dumps(ev))
    comment = tmp_path / "comment.md"
    assert (
        run("ingest-issue", "--event", str(evf), "--comment-file", str(comment), "--no-notify") == 0
    )
    assert "ok=true" in out.read_text() and "created=true" in out.read_text()
    assert "-0.05/share" in comment.read_text()
    ev["issue"]["body"] = body.replace("2\n\n### Net", "two\n\n### Net")  # type: ignore[index]
    ev["issue"]["number"] = 8  # type: ignore[index]
    evf.write_text(json.dumps(ev))
    assert (
        run("ingest-issue", "--event", str(evf), "--comment-file", str(comment), "--no-notify") == 2
    )
    assert "ok=false" in out.read_text() and "Nothing was written" in comment.read_text()
    ev["issue"]["user"]["login"] = "mallory"  # type: ignore[index]
    evf.write_text(json.dumps(ev))
    comment.unlink()
    assert (
        run("ingest-issue", "--event", str(evf), "--comment-file", str(comment), "--no-notify") == 0
    )
    assert (
        "ignored=true" in out.read_text() and not comment.exists()
    )  # strangers get no comment at all


def test_summary_report_watchdog_and_shadow_commands(
    root: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store.append(root, "alert", alert())
    assert run("summary") == 0
    assert run("report", "--weekly", "--no-notify", "--force") == 0
    assert list((root / "reports/weekly").glob("*.md"))
    assert run("shadow", alert()["id"]) == 0
    assert (
        "unmarkable" in capsys.readouterr().out
    )  # no snapshots exist yet: honest answer, not a guess
    assert run("watchdog", "--no-notify") == 1  # nothing has ever run
    assert (
        run("report", "--weekly", "--no-notify", "--require-window") == 0
    )  # outside the Sunday window (or already written): quiet


def test_compact_export_size_check_backfill_without_key(
    root: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ALPHAVANTAGE_API_KEY", raising=False)
    assert run("compact") == 0 and run("size-check") == 0
    assert (
        run("backfill", "--start", "2026-10-01", "--end", "2026-10-02") == 0
    )  # exits 0: never fails CI
    assert "ALPHAVANTAGE_API_KEY" in capsys.readouterr().out
    assert (
        run(
            "export",
            "--format",
            "csv",
            "--underlying",
            "SPY",
            "--start",
            "2026-11-01",
            "--end",
            "2026-11-02",
        )
        == 0
    )


def test_heartbeat_command(root: Path) -> None:
    from odj.common import heartbeat

    assert run("heartbeat", "demo", "--status", "ok", "--slot-key", "2026-11-02/open") == 0
    assert heartbeat.load(root)["jobs"]["demo"]["last_slot"] == "2026-11-02/open"
