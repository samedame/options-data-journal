"""Workflow / issue-form / docs sanity: valid YAML, least privilege, no shell interpolation of untrusted input."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from odj.journal import issue_forms
from tests.conftest import REPO

WORKFLOWS = sorted((REPO / ".github" / "workflows").glob("*.yml"))
TEMPLATES = sorted((REPO / ".github" / "ISSUE_TEMPLATE").glob("log-*.yml"))
UNTRUSTED = re.compile(
    r"\$\{\{\s*(github\.event\.(issue|comment|client_payload|pull_request|head_commit|review)|github\.head_ref|inputs\.)"
)


def load(p: Path) -> dict[str, Any]:
    doc = yaml.safe_load(p.read_text())
    assert isinstance(doc, dict)
    return doc


def test_all_expected_workflows_exist() -> None:
    names = {p.stem for p in WORKFLOWS}
    assert names >= {
        "snapshot",
        "ingest-alert",
        "ingest-issue",
        "weekly-report",
        "compact",
        "watchdog",
        "ci",
    }


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_workflow_is_valid_least_privilege_and_triggered(path: Path) -> None:
    doc = load(path)
    triggers = doc.get(True) or doc.get("on")  # PyYAML parses a bare `on:` key as boolean True
    assert triggers, f"{path.name}: no triggers"
    assert "permissions" in doc, f"{path.name}: top-level permissions block required"
    assert set(doc["permissions"].values()) <= {"read", "write"}
    assert doc["permissions"].get("contents") in {"read", "write"}
    for job in doc["jobs"].values():
        assert "runs-on" in job and job.get("timeout-minutes"), (
            f"{path.name}: set runs-on and a timeout"
        )


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_untrusted_context_is_interpolated_into_shell(path: Path) -> None:
    doc = load(path)
    for job in doc["jobs"].values():
        for step in job["steps"]:
            run = step.get("run", "")
            assert not UNTRUSTED.search(run), (
                f"{path.name}: step {step.get('name')!r} interpolates untrusted context into `run:`"
            )


def test_scheduling_and_dispatch_shapes() -> None:
    snap = load(REPO / ".github/workflows/snapshot.yml")
    on = snap.get(True) or snap["on"]
    assert "slot" in on["workflow_dispatch"]["inputs"]
    crons = [c["cron"] for c in on["schedule"]]
    assert len(crons) == 4 and {c.split()[1] for c in crons} == {
        "13",
        "14",
        "19",
        "20",
    }  # both DST offsets per slot
    alert = load(REPO / ".github/workflows/ingest-alert.yml")
    assert (alert.get(True) or alert["on"])["repository_dispatch"]["types"] == ["alert"]
    assert alert["concurrency"]["cancel-in-progress"] is False
    issue = load(REPO / ".github/workflows/ingest-issue.yml")
    cond = issue["jobs"]["ingest"]["if"]
    assert "github.event.issue.user.login == github.repository_owner" in cond
    assert issue["permissions"] == {"contents": "write", "issues": "write"}
    assert load(REPO / ".github/workflows/watchdog.yml")["permissions"] == {"contents": "read"}


def test_every_committing_workflow_uses_the_retrying_publisher() -> None:
    for name in ("snapshot", "ingest-alert", "ingest-issue", "weekly-report", "compact"):
        text = (REPO / ".github/workflows" / f"{name}.yml").read_text()
        assert "odj ci publish" in text and "git push" not in text


def test_workflows_pass_actionlint_when_available() -> None:
    exe = REPO / ".venv" / "bin" / "actionlint"
    if not exe.exists():
        pytest.skip("actionlint not installed")
    cp = subprocess.run([str(exe)], cwd=REPO, capture_output=True, text=True)
    assert cp.returncode == 0, cp.stdout + cp.stderr


@pytest.mark.parametrize("path", TEMPLATES, ids=lambda p: p.name)
def test_issue_form_loads_and_matches_parser_labels(path: Path) -> None:
    doc = load(path)
    assert {"name", "description", "body", "title", "labels"} <= set(doc)
    kind = path.stem.removeprefix("log-")
    assert doc["labels"] == [f"log-{kind}"] and doc["title"].startswith(f"[{kind}]")
    form_labels = {b["attributes"]["label"] for b in doc["body"] if b["type"] != "markdown"}
    assert form_labels == set(issue_forms.LABELS[kind].values()), (
        "form field labels must equal the parser's expected headings"
    )
    ids = [b["id"] for b in doc["body"] if b["type"] != "markdown"]
    assert len(ids) == len(set(ids))
    for b in doc["body"]:
        if b["type"] == "dropdown":
            assert b["attributes"]["options"] and b["validations"]["required"] is True


def test_dropdown_options_match_schema_enums() -> None:
    import json

    def options(kind: str, label: str) -> list[str]:
        doc = load(REPO / f".github/ISSUE_TEMPLATE/log-{kind}.yml")
        return next(
            b["attributes"]["options"]
            for b in doc["body"]
            if b.get("attributes", {}).get("label") == label
        )

    ex = json.loads((REPO / "schemas/exit.schema.json").read_text())["properties"]["reason"]["enum"]
    sk = json.loads((REPO / "schemas/skip.schema.json").read_text())["properties"]["reason"]["enum"]
    fl = json.loads((REPO / "schemas/fill.schema.json").read_text())["properties"]["order_type"][
        "enum"
    ]
    assert (
        options("exit", "Exit reason") == ex
        and options("skip", "Skip reason") == sk
        and options("fill", "Order type") == fl
    )


def test_secrets_hygiene() -> None:
    gi = (REPO / ".gitignore").read_text()
    assert ".env" in gi.splitlines() and "!.env.example" in gi
    assert (REPO / ".env.example").exists()
    pattern = re.compile(
        r"discord(app)?\.com/api/webhooks/\d+|ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}"
    )
    for p in REPO.rglob("*"):
        if (
            p.is_file()
            and ".git/" not in str(p)
            and ".venv" not in p.parts
            and p.suffix in {".py", ".md", ".yml", ".yaml", ".json", ".toml", ".txt", ".example"}
        ):
            assert not pattern.search(p.read_text(errors="ignore")), f"secret-looking string in {p}"


@pytest.mark.parametrize(
    ("doc", "needles"),
    [
        (
            "README.md",
            [
                "open-scheduler",
                "workflow_dispatch",
                "reset-test-data",
                "DISCORD_WEBHOOK_URL",
                "minutes",
                "Robinhood",
            ],
        ),
        ("DECISIONS.md", ["size per day", "OptionsDX", "concurrency", "stop"]),
        (
            "VERIFY.md",
            ["UNVERIFIED-LIVE", "import-robinhood", "odj snapshot --underlying SPY --dry-run"],
        ),
        (
            "INTEGRATION.md",
            [
                "JOURNAL_DISPATCH_TOKEN",
                "fine-grained",
                "dry-run",
                "frontier5-bot",
                "Apex Chronos",
                "tier1-bot",
                "vol-regime-gate",
            ],
        ),
        (
            "BOTDECK_HANDOFF.md",
            ["data/summary/latest.json", "contents API", "Execution quality", "schema_version"],
        ),
    ],
)
def test_docs_exist_and_are_specific(doc: str, needles: list[str]) -> None:
    text = (REPO / doc).read_text()
    assert len(text) > 1500, f"{doc} looks like boilerplate"
    for n in needles:
        assert n in text, f"{doc} should mention {n!r}"
