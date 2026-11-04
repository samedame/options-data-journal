"""odj_client.py - SINGLE-FILE alert sender. Copy this file into any bot repo (stdlib only).

    from odj_client import send_alert, make_alert_id
    ok = send_alert({...})          # never raises; returns False on any failure

Environment (set as secrets in the bot repo):
    JOURNAL_REPO            "samedame/options-data-journal"
    JOURNAL_DISPATCH_TOKEN  fine-grained PAT, this one repo only, permission needed to send
                            repository_dispatch events (see INTEGRATION.md)
    ODJ_DRY_RUN=1           (optional) validate + print, send nothing

How it travels: GitHub caps repository_dispatch `client_payload` at 10 top-level properties, so the
alert is sent as ONE JSON-string property: {"alert": "<json>"}. The journal repo validates it
against schemas/alert.schema.json and rejects invalid payloads loudly (Discord). The checks below
are a light pre-flight only.

Dry run from a shell:   python odj_client.py --dry-run alert.json
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

STRUCTURES = {
    "put_credit_spread",
    "call_credit_spread",
    "iron_condor",
    "long_straddle",
    "long_strangle",
    "long_call",
    "long_put",
    "short_put",
    "covered_call",
    "equity_long",
    "equity_short",
    "other",
}
PRICE_BASIS = {"mid", "bid_ask", "last", "model"}
_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+-\d{8}T\d{6}Z-\d+$")
_REQUIRED = (
    "id",
    "ts_utc",
    "bot",
    "strategy",
    "underlying",
    "structure",
    "legs",
    "alert_net_price",
    "alert_price_basis",
)
TIMEOUT_SECONDS = 8.0


def make_alert_id(bot: str, n: int = 1, now: datetime | None = None) -> str:
    """`{bot}-{YYYYMMDDTHHMMSSZ}-{n}`. Use a stable n per alert in one run so retries stay idempotent."""
    t = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return f"{bot}-{t.strftime('%Y%m%dT%H%M%SZ')}-{n}"


def leg(
    side: str, typ: str, strike: float | None = None, expiry: str | None = None, ratio: int = 1
) -> dict[str, Any]:
    """One leg. side: buy|sell, typ: call|put|stock, expiry: 'YYYY-MM-DD'."""
    out: dict[str, Any] = {"side": side, "type": typ, "ratio": ratio}
    if typ != "stock":
        out["strike"] = float(strike) if strike is not None else None
        out["expiry"] = expiry
    return out


def build_alert(
    bot: str,
    strategy: str,
    underlying: str,
    structure: str,
    legs: list[dict[str, Any]],
    net_price: float,
    *,
    basis: str = "mid",
    contracts: int | None = None,
    max_profit: float | None = None,
    max_loss: float | None = None,
    profit_target_pct: float | None = None,
    stop_multiple: float | None = None,
    dte_exit: int | None = None,
    time_exit: Any = None,
    confidence: float | None = None,
    gate: str | None = None,
    notes: str | None = None,
    n: int = 1,
    now: datetime | None = None,
    test: bool = False,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assemble a schema-shaped alert. net_price is PER SHARE, credit positive / debit negative;
    max_profit / max_loss are dollars PER CONTRACT (positive)."""
    t = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    rules = {
        "profit_target_pct_of_credit": profit_target_pct,
        "stop_loss_multiple_of_credit": stop_multiple,
        "dte_exit": dte_exit,
        "time_exit": time_exit,
    }
    alert: dict[str, Any] = {
        "id": make_alert_id(bot, n, t),
        "ts_utc": t.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bot": bot,
        "strategy": strategy,
        "underlying": underlying.upper(),
        "structure": structure,
        "legs": legs,
        "contracts_suggested": contracts,
        "alert_net_price": round(float(net_price), 4),
        "alert_price_basis": basis,
        "max_profit": max_profit,
        "max_loss": max_loss,
        "exit_rules": {k: v for k, v in rules.items() if v is not None},
        "confidence_score": confidence,
        "regime_gate_state": gate,
        "notes": notes,
    }
    if meta:
        alert["meta"] = meta
    if test:
        alert["test"] = True
    return alert


def validate_light(alert: Any) -> list[str]:
    """Cheap structural pre-flight. The authoritative validation happens in the journal repo."""
    if not isinstance(alert, dict):
        return ["alert must be a dict"]
    errs = [f"missing field: {k}" for k in _REQUIRED if k not in alert]
    if errs:
        return errs
    if not isinstance(alert["id"], str) or not _ID_RE.match(alert["id"]):
        errs.append("id must look like {bot}-{YYYYMMDDTHHMMSSZ}-{n}")
    if alert["structure"] not in STRUCTURES:
        errs.append(f"structure must be one of {sorted(STRUCTURES)}")
    if alert["alert_price_basis"] not in PRICE_BASIS:
        errs.append(f"alert_price_basis must be one of {sorted(PRICE_BASIS)}")
    if not isinstance(alert["legs"], list) or not alert["legs"]:
        errs.append("legs must be a non-empty list")
    else:
        for i, leg in enumerate(alert["legs"]):
            if (
                not isinstance(leg, dict)
                or leg.get("side") not in ("buy", "sell")
                or leg.get("type") not in ("call", "put", "stock")
            ):
                errs.append(f"legs[{i}] needs side buy|sell and type call|put|stock")
            elif leg["type"] != "stock" and not (leg.get("strike") and leg.get("expiry")):
                errs.append(f"legs[{i}] option leg needs strike and expiry (YYYY-MM-DD)")
    if isinstance(alert["alert_net_price"], bool) or not isinstance(
        alert["alert_net_price"], (int, float)
    ):
        errs.append("alert_net_price must be a number (per share, credit +, debit -)")
    cs = alert.get("confidence_score")
    if cs is not None and not (isinstance(cs, (int, float)) and 1 <= cs <= 100):
        errs.append("confidence_score must be null or 1..100")
    try:
        json.dumps(alert)
    except (TypeError, ValueError):
        errs.append("alert is not JSON-serialisable")
    return errs


def send_alert(
    alert: dict[str, Any], *, dry_run: bool | None = None, timeout: float = TIMEOUT_SECONDS
) -> bool:
    """Send one alert to the journal repo. Never raises into the host bot. Returns True on success
    (or on a valid dry run), False otherwise."""
    try:
        if dry_run is None:
            dry_run = os.environ.get("ODJ_DRY_RUN", "").strip().lower() in ("1", "true", "yes")
        errs = validate_light(alert)
        if errs:
            _log(f"alert rejected locally: {'; '.join(errs)}")
            return False
        payload = json.dumps(
            {"event_type": "alert", "client_payload": {"alert": json.dumps(alert, sort_keys=True)}}
        )
        if dry_run:
            print(f"[odj dry-run] valid; would send {len(payload)} bytes for {alert['id']}:")
            print(json.dumps(alert, indent=2, sort_keys=True))
            return True
        repo = os.environ.get("JOURNAL_REPO", "").strip()
        token = os.environ.get("JOURNAL_DISPATCH_TOKEN", "").strip()
        if not repo or not token:
            _log("JOURNAL_REPO / JOURNAL_DISPATCH_TOKEN not set; alert not sent")
            return False
        req = urllib.request.Request(
            f"https://api.github.com/repos/{repo}/dispatches",
            data=payload.encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "odj-client",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            ok = 200 <= resp.status < 300
        if not ok:
            _log(f"dispatch returned HTTP {resp.status}")
        return bool(ok)
    except urllib.error.HTTPError as exc:
        _log(f"dispatch failed: HTTP {exc.code}")  # never log the token or request headers
        return False
    except Exception as exc:
        _log(f"dispatch failed: {type(exc).__name__}")
        return False


def _log(msg: str) -> None:
    try:
        print(f"[odj_client] {msg}", file=sys.stderr)
    except Exception:
        pass


def _main(argv: list[str]) -> int:
    args = [a for a in argv if a != "--dry-run"]
    if not args:
        print("usage: python odj_client.py [--dry-run] alert.json", file=sys.stderr)
        return 2
    with open(args[0], encoding="utf-8") as fh:
        alert = json.load(fh)
    return 0 if send_alert(alert, dry_run=("--dry-run" in argv) or None) else 1


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
