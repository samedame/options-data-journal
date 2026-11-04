"""`odj selftest`: the whole pipeline, offline, on SYNTHETIC data, in a temp directory.

snapshot (window + idempotency) -> alert ingest -> fill + exit ingest -> shadow tracking ->
weekly report -> summary JSON -> reset-test-data. Nothing touches the network or this repo's data.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from odj.common.calendars import trading_days
from odj.common.config import find_root
from odj.common.timeutil import et_datetime
from odj.journal import store
from odj.journal.ingest import ingest_alert_payload
from odj.journal.reports import generate_weekly
from odj.journal.summary import validate_summary, write_summary
from odj.journal.tracking import track_alerts
from odj.snapshot.providers.synthetic import SyntheticProvider
from odj.snapshot.run import run_snapshot
from odj.snapshot.store import load_chains, load_market

FIRST_DAY = date(2026, 10, 5)  # a Monday; far enough in the past that the calendar is settled
N_DAYS = 12


def _utc(d: date, hhmm: str) -> datetime:
    return et_datetime(d, hhmm).astimezone(UTC)


def _tree_hash(path: Path) -> str:
    h = hashlib.sha256()
    for p in sorted(path.rglob("*")):
        if p.is_file() and not p.name.endswith(".lock"):
            h.update(str(p.relative_to(path)).encode())
            h.update(p.read_bytes())
    return h.hexdigest()


class _Checks:
    def __init__(self) -> None:
        self.failed = 0

    def check(self, name: str, ok: bool, detail: str = "") -> None:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}{(' - ' + detail) if detail else ''}")
        if not ok:
            self.failed += 1


def run_selftest() -> int:
    src_root = find_root()
    tmp = Path(tempfile.mkdtemp(prefix="odj-selftest-"))
    saved_env = {k: os.environ.get(k) for k in ("ODJ_ROOT", "DISCORD_WEBHOOK_URL")}
    try:
        for d in ("config", "schemas"):
            shutil.copytree(src_root / d, tmp / d)
        os.environ["ODJ_ROOT"] = str(tmp)
        os.environ.pop("DISCORD_WEBHOOK_URL", None)  # selftest never posts anywhere
        return _scenario(tmp)
    finally:
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(tmp, ignore_errors=True)


def _scenario(root: Path) -> int:
    c = _Checks()
    print(f"odj selftest (SYNTHETIC data, temp dir {root})")
    clock = {"now": _utc(FIRST_DAY, "09:46")}
    spot = {"v": 540.0}
    prov = SyntheticProvider(spot=lambda _u: spot["v"], now=lambda: clock["now"], n_expiries=10)
    days = trading_days(FIRST_DAY, date(2026, 10, 30))[:N_DAYS]

    def snap(
        d: date, slot: str, hhmm: str, px: float, *, underlyings: list[str] | None = None
    ) -> Any:
        clock["now"] = _utc(d, hhmm)
        spot["v"] = px
        prov.today = d
        return run_snapshot(
            root,
            provider=prov,
            slot="auto",
            now=clock["now"],
            notify=False,
            underlyings=underlyings,
        )

    # --- day 0 snapshots (SPY only to keep it fast), window logic and idempotency
    r0 = snap(days[0], "open", "09:46", 540.0, underlyings=["SPY"])
    c.check(
        "open slot snapshot stored",
        any(r.status == "ok" for r in r0.results),
        str([(r.underlying, r.status, r.rows) for r in r0.results]),
    )
    off = run_snapshot(
        root,
        provider=prov,
        slot="auto",
        now=_utc(days[0], "11:30"),
        notify=False,
        underlyings=["SPY"],
    )
    c.check("outside the slot window -> quiet no-op", off.skipped_reason is not None)
    h = _tree_hash(root / "data")
    again = snap(days[0], "open", "09:47", 540.0, underlyings=["SPY"])
    c.check(
        "re-run same slot is a no-op",
        all(r.status == "skipped_exists" for r in again.results) and _tree_hash(root / "data") == h,
    )

    chain = load_chains("SPY", days[0], days[0], usable_only=False, root=root)
    c.check(
        "chain has greeks + flags",
        {"iv_calc", "delta", "parity_violation", "usable"} <= set(chain.columns)
        and bool(chain["delta"].notna().any()),
    )
    mkt = load_market(days[0], days[0], root=root)
    c.check("market file written", len(mkt) == 1 and float(mkt["vix"].iloc[0]) == 16.0)

    # --- alert: 530/525 bull put spread, priced from the stored day-0 chain
    exp = (
        sorted({e for e in chain["expiry"] if (e.date() - days[0]).days >= 14})[0]
        .date()
        .isoformat()
    )

    def mid(k: float) -> float:
        row = chain[(chain["strike"] == k) & (chain["right"] == "P") & (chain["expiry"] == exp)]
        return float(row["mid"].iloc[0])

    credit = round(mid(530.0) - mid(525.0), 2)
    alert_ts = _utc(days[0], "10:00")
    alert: dict[str, Any] = {
        "id": "selftest-bot-20261005T140000Z-1",
        "ts_utc": alert_ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bot": "selftest-bot",
        "strategy": "S2",
        "underlying": "SPY",
        "structure": "put_credit_spread",
        "legs": [
            {"side": "sell", "type": "put", "strike": 530.0, "expiry": exp},
            {"side": "buy", "type": "put", "strike": 525.0, "expiry": exp},
        ],
        "contracts_suggested": 2,
        "alert_net_price": credit,
        "alert_price_basis": "mid",
        "max_profit": round(credit * 100, 2),
        "max_loss": round((5 - credit) * 100, 2),
        "exit_rules": {"profit_target_pct_of_credit": 50, "stop_loss_multiple_of_credit": 2.0},
        "confidence_score": 72,
        "regime_gate_state": "on",
        "test": True,
    }
    out = ingest_alert_payload(root, json.dumps(alert), notify=False)
    c.check("alert ingested", out.ok and out.created, out.message)
    dup = ingest_alert_payload(root, json.dumps(alert), notify=False)
    c.check("duplicate alert is idempotent", dup.ok and not dup.created)
    bad = ingest_alert_payload(
        root,
        json.dumps({**alert, "id": "selftest-bot-20261005T140001Z-2", "structure": "nope"}),
        notify=False,
    )
    c.check(
        "invalid alert rejected and not written",
        (not bad.ok) and len(store.read_all(root, "alert")) == 1,
    )

    # --- remaining days: spot drifts up, theta decays the spread
    for i, d in enumerate(days):
        px = 540.0 + 0.4 * i
        if i > 0:
            snap(d, "open", "09:46", px, underlyings=["SPY"])
        snap(d, "close", "15:51", px + 0.2, underlyings=["SPY"])
    n_snap = len(list((root / "data" / "chains" / "SPY").glob("*/*.parquet")))
    c.check(
        "two snapshots per trading day stored",
        n_snap == 2 * len(days),
        f"{n_snap} files over {len(days)} days",
    )

    # --- shadow tracker
    res = track_alerts(root, store.read_all(root, "alert"), "mid")[alert["id"]]
    c.check(
        "shadow tracker closes the spread mechanically",
        res.status == "closed",
        f"{res.status} {res.exit_reason} pnl={res.theoretical_pnl}",
    )
    cons = track_alerts(root, store.read_all(root, "alert"), "conservative")[alert["id"]]
    c.check(
        "conservative mark mode also resolves the alert",
        cons.status in ("closed", "open") and cons.n_marks > 0,
        f"{cons.status} {cons.exit_reason} pnl={cons.theoretical_pnl}",
    )

    # --- fill + exit (slightly worse than alert / theoretical)
    fill = {
        "id": "fill-selftest-1",
        "alert_id": alert["id"],
        "ts_utc": _utc(days[0], "10:03").strftime("%Y-%m-%dT%H:%M:%SZ"),
        "legs_actual": alert["legs"],
        "contracts": 2,
        "net_price": round(credit - 0.03, 2),
        "fees_total": 1.30,
        "order_type": "limit",
        "source": "cli",
        "test": True,
    }
    _, created = store.append(root, "fill", fill)
    c.check("fill journaled", created)
    if (
        res.status == "closed"
        and res.theoretical_exit_price is not None
        and res.theoretical_exit_ts
    ):
        ex = {
            "id": "exit-selftest-1",
            "fill_id": "fill-selftest-1",
            "ts_utc": res.theoretical_exit_ts,
            "contracts": 2,
            "net_price": round(res.theoretical_exit_price - 0.02, 2),
            "fees_total": 1.30,
            "reason": "profit_target" if res.exit_reason == "profit_target" else "manual",
            "source": "cli",
            "test": True,
        }
        _, created = store.append(root, "exit", ex)
        c.check("exit journaled", created)

    # --- report + summary
    rep = generate_weekly(root, now=_utc(date(2026, 10, 18), "19:30"), notify=False)
    md = (root / rep["path"]).read_text() if rep.get("path") else ""
    c.check(
        "weekly report written (md + json)",
        rep["status"] == "written" and (root / "reports/weekly" / f"{rep['week']}.json").exists(),
    )
    c.check(
        "report carries the data-limits footer",
        "delayed" in md.lower() and "snapshot granularity" in md.lower(),
    )
    c.check("report flags INSUFFICIENT SAMPLE for tiny n", "INSUFFICIENT SAMPLE" in md)
    doc = write_summary(root, now=_utc(date(2026, 10, 18), "19:35"))
    c.check("summary matches its schema", not validate_summary(root, doc))
    c.check(
        "summary shows data collection",
        doc["data_collection"]["SPY"]["days_collected"] == len(days),
    )
    rolling = doc["rolling"]["90d"]
    c.check(
        "summary has rolling 90d metrics without error",
        rolling.get("error") is None and rolling["alerts"] == 1,
        str(rolling.get("error")),
    )

    # --- reset-test-data removes only tagged records
    store.append(
        root,
        "skip",
        {"id": "skip-keep", "alert_id": alert["id"], "ts_utc": alert["ts_utc"], "reason": "other"},
    )
    counts = store.remove_test_records(root)
    c.check(
        "reset-test-data removed tagged records only",
        counts["alert"] == 1 and counts["fill"] == 1 and len(store.read_all(root, "skip")) == 1,
        str(counts),
    )

    print("PASS" if c.failed == 0 else f"FAIL ({c.failed} check(s) failed)")
    return 0 if c.failed == 0 else 1
