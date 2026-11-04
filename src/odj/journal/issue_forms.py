"""Parse GitHub Issue-Form bodies into journal records.

The issue body is treated strictly as DATA: it is parsed with string operations and regexes only,
never passed to a shell, `eval`, or a template engine. Free text ends up only inside JSON strings.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from odj.common.timeutil import normalize_ts
from odj.journal import store

NO_RESPONSE = "_No response_"
MAX_FIELD = 2000

LABELS: dict[str, dict[str, str]] = {
    "fill": {
        "alert_id": "Alert ID",
        "ts": "Filled at (UTC)",
        "legs": "Legs",
        "contracts": "Contracts",
        "net_price": "Net price per share",
        "fees": "Fees total (USD)",
        "order_type": "Order type",
        "limit_price": "Limit price",
        "notes": "Notes",
        "test": "Test record",
    },
    "exit": {
        "fill_id": "Fill ID",
        "alert_id": "Alert ID (if Fill ID unknown)",
        "ts": "Exited at (UTC)",
        "contracts": "Contracts",
        "net_price": "Net price per share",
        "fees": "Fees total (USD)",
        "reason": "Exit reason",
        "notes": "Notes",
        "test": "Test record",
    },
    "skip": {
        "alert_id": "Alert ID",
        "reason": "Skip reason",
        "ts": "Skipped at (UTC)",
        "notes": "Notes",
        "test": "Test record",
    },
}
KIND_LABEL = {"log-fill": "fill", "log-exit": "exit", "log-skip": "skip"}
_LEG_RE = re.compile(
    r"^(?P<side>buy|sell)\s+(?P<type>call|put)\s+(?P<strike>\d+(?:\.\d+)?)\s+(?P<expiry>\d{4}-\d{2}-\d{2})(?:\s+x(?P<ratio>\d+))?$",
    re.IGNORECASE,
)
_STOCK_RE = re.compile(r"^(?P<side>buy|sell)\s+stock(?:\s+x(?P<ratio>\d+))?$", re.IGNORECASE)


class FormError(ValueError):
    """The issue body could not be turned into a valid record."""


def _known_labels() -> set[str]:
    return {lab for form in LABELS.values() for lab in form.values()}


def parse_sections(body: str) -> dict[str, str]:
    """Split an issue-form body on '### Heading' lines. Unknown text before the first heading is ignored.

    A KNOWN field heading that appears twice is rejected: GitHub renders each form field exactly once,
    so a repeat means free text (e.g. in Notes) is imitating a field - treated as malformed/injected.
    """
    known = _known_labels()
    sections: dict[str, str] = {}
    seen_known: set[str] = set()
    current: str | None = None
    buf: list[str] = []
    for line in body.replace("\r\n", "\n").split("\n"):
        if line.startswith("### "):
            if current is not None:
                sections[current] = "\n".join(buf).strip()
            current = line[4:].strip()
            if current in known:
                if current in seen_known:
                    raise FormError(
                        f"section '{current}' appears more than once; refusing an ambiguous body"
                    )
                seen_known.add(current)
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip()
    return {k: ("" if v == NO_RESPONSE else v[: MAX_FIELD * 3]) for k, v in sections.items()}


def detect_kind(issue_labels: list[str], sections: dict[str, str]) -> str:
    from_labels = {KIND_LABEL[x] for x in issue_labels if x in KIND_LABEL}
    from_body = {
        k
        for k, lab in (
            ("fill", "Order type"),
            ("exit", "Exit reason"),
            ("skip", "Skip reason"),
        )
        if lab in sections
    }
    if len(from_labels) == 1 and (not from_body or from_body == from_labels):
        return next(iter(from_labels))
    if not from_labels and len(from_body) == 1:
        return next(iter(from_body))
    raise FormError(
        f"cannot tell which form this is (labels={sorted(from_labels)}, fields suggest {sorted(from_body)})"
    )


def _field(sections: dict[str, str], kind: str, key: str) -> str:
    v = sections.get(LABELS[kind][key], "").strip()
    if len(v) > MAX_FIELD:
        raise FormError(f"'{LABELS[kind][key]}' is longer than {MAX_FIELD} characters")
    return v


def _num(raw: str, label: str) -> float:
    s = raw.replace("$", "").replace(",", "").replace("−", "-").strip()
    try:
        return float(s)
    except ValueError:
        raise FormError(f"'{label}' must be a number, got {raw[:40]!r}") from None


def _int(raw: str, label: str) -> int:
    try:
        n = int(raw.strip())
    except ValueError:
        raise FormError(f"'{label}' must be a whole number, got {raw[:40]!r}") from None
    return n


def parse_legs(text: str) -> list[dict[str, Any]]:
    legs: list[dict[str, Any]] = []
    for ln in (x.strip() for x in text.split("\n")):
        if not ln:
            continue
        if m := _LEG_RE.match(ln):
            legs.append(
                {
                    "side": m["side"].lower(),
                    "type": m["type"].lower(),
                    "strike": float(m["strike"]),
                    "expiry": m["expiry"],
                    "ratio": int(m["ratio"] or 1),
                }
            )
        elif m := _STOCK_RE.match(ln):
            legs.append({"side": m["side"].lower(), "type": "stock", "ratio": int(m["ratio"] or 1)})
        else:
            raise FormError(
                f"cannot parse leg line {ln[:60]!r}; use e.g. 'sell put 540 2026-11-20' or 'buy call 560 2026-11-20 x2'"
            )
    return legs


def _is_checked(sections: dict[str, str], kind: str) -> bool:
    return "[x]" in sections.get(LABELS[kind]["test"], "").lower()


def _ts(sections: dict[str, str], kind: str, default: str) -> str:
    raw = _field(sections, kind, "ts")
    try:
        return normalize_ts(raw or default)
    except ValueError:
        raise FormError(
            f"timestamp must be ISO-8601 with timezone (e.g. 2026-11-03T14:05:00Z), got {raw[:40]!r}"
        ) from None


def build_record(
    root: Path,
    kind: str,
    sections: dict[str, str],
    *,
    issue_number: int,
    created_at: str,
) -> dict[str, Any]:
    notes = _field(sections, kind, "notes") or None
    rec: dict[str, Any]
    if kind == "fill":
        alert_id = _field(sections, kind, "alert_id") or None
        alert = store.get(root, "alert", alert_id) if alert_id else None
        if alert_id and alert is None:
            raise FormError(f"unknown Alert ID {alert_id[:80]!r}")
        legs = parse_legs(sections.get(LABELS[kind]["legs"], ""))
        if not legs:
            if alert is None:
                raise FormError("provide Legs, or an Alert ID whose legs should be used")
            legs = [
                {k: leg[k] for k in ("side", "type", "strike", "expiry", "ratio") if k in leg}
                for leg in alert["legs"]
            ]
        rec = {
            "id": f"fill-gh{issue_number}",
            "alert_id": alert_id,
            "ts_utc": _ts(sections, kind, created_at),
            "broker": "robinhood",
            "legs_actual": legs,
            "contracts": _int(_field(sections, kind, "contracts"), "Contracts"),
            "net_price": _num(_field(sections, kind, "net_price"), "Net price per share"),
            "fees_total": _num(_field(sections, kind, "fees") or "0", "Fees total (USD)"),
            "order_type": (_field(sections, kind, "order_type") or "limit").lower(),
            "limit_price": (
                _num(_field(sections, kind, "limit_price"), "Limit price")
                if _field(sections, kind, "limit_price")
                else None
            ),
            "source": "issue",
            "notes": notes,
        }
    elif kind == "exit":
        fill_id = _field(sections, kind, "fill_id")
        if not fill_id:
            alert_id = _field(sections, kind, "alert_id")
            if not alert_id:
                raise FormError("provide Fill ID (or an Alert ID with exactly one fill)")
            matches = [f for f in store.read_all(root, "fill") if f.get("alert_id") == alert_id]
            if len(matches) != 1:
                raise FormError(
                    f"Alert ID {alert_id[:60]!r} has {len(matches)} fills; give the Fill ID explicitly"
                )
            fill_id = matches[0]["id"]
        rec = {
            "id": f"exit-gh{issue_number}",
            "fill_id": fill_id,
            "ts_utc": _ts(sections, kind, created_at),
            "contracts": _int(_field(sections, kind, "contracts"), "Contracts"),
            "net_price": _num(_field(sections, kind, "net_price"), "Net price per share"),
            "fees_total": _num(_field(sections, kind, "fees") or "0", "Fees total (USD)"),
            "reason": (_field(sections, kind, "reason") or "manual").lower().replace(" ", "_"),
            "source": "issue",
            "notes": notes,
        }
    else:
        rec = {
            "id": f"skip-gh{issue_number}",
            "alert_id": _field(sections, kind, "alert_id"),
            "ts_utc": _ts(sections, kind, created_at),
            "reason": (_field(sections, kind, "reason") or "other").lower().replace(" ", "_"),
            "source": "issue",
            "notes": notes,
        }
    if _is_checked(sections, kind):
        rec["test"] = True
    return rec
