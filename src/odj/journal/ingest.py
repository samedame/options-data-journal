"""Ingest paths used by the workflows (repository_dispatch alerts, issue forms) and the CLI."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from odj.common import heartbeat
from odj.common.discord import post as discord_post
from odj.common.schema import ValidationFailed
from odj.journal import issue_forms, store
from odj.journal.slippage import entry_slippage, multiplier_for_legs

log = logging.getLogger("odj.ingest")
MAX_PAYLOAD_BYTES = 200_000


@dataclass
class Outcome:
    ok: bool
    kind: str | None = None
    record_id: str | None = None
    created: bool = False
    message: str = ""
    errors: list[str] = field(default_factory=list)


def _refresh(root: Path, job: str) -> None:
    try:
        heartbeat.record(root, job, status="ok")
        from odj.journal.summary import write_summary

        write_summary(root)
    except Exception as exc:  # bookkeeping must never fail an ingest
        log.warning("post-ingest summary failed: %s", exc)


def ingest_alert_payload(
    root: Path, raw: Any, *, notify: bool = True, dry_run: bool = False
) -> Outcome:
    """`raw` is the repository_dispatch client_payload['alert']: a JSON string (or already a dict)."""
    try:
        if isinstance(raw, str):
            if len(raw.encode()) > MAX_PAYLOAD_BYTES:
                raise ValueError("alert payload too large")
            alert = json.loads(raw)
        else:
            alert = raw
        if not isinstance(alert, dict):
            raise ValueError("alert payload must be a JSON object")
    except (ValueError, json.JSONDecodeError) as exc:
        msg = f"rejected alert: unreadable payload ({exc})"
        if notify:
            discord_post(msg, "fail")
        return Outcome(False, "alert", message=msg, errors=[str(exc)])
    try:
        if dry_run:
            from odj.common.schema import validate

            rec = validate("alert", alert, root)
            return Outcome(
                True,
                "alert",
                rec["id"],
                False,
                f"dry-run: alert {rec['id']} is valid (not written)",
            )
        rec, created = store.append(root, "alert", alert)
    except (ValidationFailed, store.JournalError) as exc:
        errs = exc.errors if isinstance(exc, ValidationFailed) else [str(exc)]
        who = str(alert.get("bot", "?"))[:40]
        msg = f"rejected alert from `{who}`: " + "; ".join(errs[:4])
        if notify:
            discord_post(msg, "fail")
        return Outcome(False, "alert", str(alert.get("id", ""))[:80], message=msg, errors=errs)
    verb = "journaled" if created else "already journaled (duplicate ignored)"
    msg = f"alert {rec['id']} ({rec['bot']} {rec['underlying']} {rec['structure']}) {verb}"
    if notify:
        discord_post(msg, "info")
    if created:
        _refresh(root, "ingest-alert")
    return Outcome(True, "alert", rec["id"], created, msg)


def ingest_alert_event(root: Path, event_path: Path, *, notify: bool = True) -> Outcome:
    event = json.loads(event_path.read_text(encoding="utf-8"))
    payload = (event.get("client_payload") or {}).get("alert")
    if payload is None:
        msg = "rejected: repository_dispatch payload has no 'alert' property"
        if notify:
            discord_post(msg, "fail")
        return Outcome(False, "alert", message=msg, errors=[msg])
    return ingest_alert_payload(root, payload, notify=notify)


def _summarize_fill(root: Path, rec: dict[str, Any]) -> str:
    lines = [
        f"Logged fill `{rec['id']}`: {rec['contracts']} contract(s) at net {rec['net_price']:+.2f}/share."
    ]
    alert = store.get(root, "alert", rec["alert_id"]) if rec.get("alert_id") else None
    if alert:
        mult = multiplier_for_legs(rec["legs_actual"])
        s = entry_slippage(
            float(alert["alert_net_price"]),
            float(rec["net_price"]),
            multiplier=mult,
            max_loss_usd=alert.get("max_loss"),
        )
        pct = "n/a" if s["pct_of_alert_premium"] is None else f"{s['pct_of_alert_premium']:+.1f}%"
        pml = "" if s["pct_of_max_loss"] is None else f", {s['pct_of_max_loss']:+.1f}% of max loss"
        lines.append(
            f"Entry slippage vs alert `{alert['id']}` (alert net {alert['alert_net_price']:+.2f}): "
            f"**{s['per_share']:+.2f}/share = {s['usd_per_contract']:+.2f} USD/contract**, {pct} of alert premium{pml} "
            "(negative = worse than the alert)."
        )
    else:
        lines.append("No alert linked, so no slippage computed.")
    return "\n".join(lines)


def ingest_issue_event(root: Path, event: dict[str, Any], *, notify: bool = True) -> Outcome:
    issue = event.get("issue") or {}
    author = str((issue.get("user") or {}).get("login", "")).lower()
    owner = str(((event.get("repository") or {}).get("owner") or {}).get("login", "")).lower()
    if not author or author != owner:
        # Silent for the workflow comment path: caller must not comment on strangers' issues
        return Outcome(
            False,
            None,
            message="ignored: issue author is not the repository owner",
            errors=["not owner"],
        )
    labels = [str(lb.get("name", "")) for lb in (issue.get("labels") or []) if isinstance(lb, dict)]
    body = str(issue.get("body") or "")
    number = int(issue.get("number", 0))
    kind: str | None = None
    try:
        sections = issue_forms.parse_sections(body)
        kind = issue_forms.detect_kind(labels, sections)
        rec = issue_forms.build_record(
            root, kind, sections, issue_number=number, created_at=str(issue.get("created_at") or "")
        )
        stored, created = store.append(root, kind, rec)
    except (issue_forms.FormError, ValidationFailed, store.JournalError) as exc:
        errs = exc.errors if isinstance(exc, ValidationFailed) else [str(exc)]
        msg = (
            "Could not log this entry. Nothing was written. Fix the issue (edit it, then re-run by opening a new one):\n"
            + "\n".join(f"- `{e[:200]}`" for e in errs[:8])
        )
        return Outcome(False, kind, message=msg, errors=errs)
    if kind == "fill":
        text = _summarize_fill(root, stored)
    elif kind == "exit":
        text = f"Logged exit `{stored['id']}` for fill `{stored['fill_id']}`: {stored['contracts']} contract(s) at net {stored['net_price']:+.2f}/share ({stored['reason']})."
    else:
        text = (
            f"Logged skip `{stored['id']}` for alert `{stored['alert_id']}` ({stored['reason']})."
        )
    if not created:
        text += "\n(This record already existed; nothing changed.)"
    if stored.get("test"):
        text += "\nTagged as a TEST record (`odj reset-test-data` removes it)."
    if created:
        _refresh(root, "ingest-issue")
        if notify:
            discord_post(f"{kind} {stored['id']} logged via issue #{number}", "info")
    return Outcome(True, kind, stored["id"], created, text)


def write_github_output(pairs: dict[str, str]) -> None:
    """Write simple key=value outputs for later workflow steps. Values never contain issue text."""
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as fh:
        for k, v in pairs.items():
            fh.write(f"{k}={v}\n")
