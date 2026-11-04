"""Robinhood account-activity CSV import (UNVERIFIED-LIVE: column names / trans codes are a hypothesis).

Pipeline: tolerant header mapping -> option rows -> group legs into spreads -> classify structure ->
auto-match to alerts with a confidence score -> append fills/exits. Anything not matched with
enough confidence goes to journal/unmatched.json for manual review; NOTHING is ever force-matched.
Re-importing the same file changes nothing (deterministic ids + existence checks).

Limitation: the CSV has dates but no times. Imported timestamps are set to a configured ET time and
marked ts_precision='date', so they are excluded from latency statistics.
"""

from __future__ import annotations

import csv
import hashlib
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from odj.common.config import find_root, journal_cfg
from odj.common.io import read_json, write_json
from odj.common.timeutil import ET, et_datetime, fmt_ts
from odj.journal import store
from odj.journal.positions import derive_positions

DESC_RE = re.compile(
    r"(?P<und>[A-Z][A-Z0-9.]*)\s+(?P<m>\d{1,2})/(?P<d>\d{1,2})/(?P<y>\d{2,4})\s+(?P<right>Call|Put)\s+\$?(?P<strike>[\d,]+(?:\.\d+)?)",
    re.IGNORECASE,
)


class RobinhoodFormatError(ValueError):
    pass


@dataclass(frozen=True)
class RhRow:
    line: int
    day: date
    und: str
    expiry: str
    right: str  # call | put
    strike: float
    code: str
    qty: int
    price: float | None
    amount: float | None
    digest: str


@dataclass
class ImportReport:
    path: str
    rows_read: int = 0
    rows_option: int = 0
    rows_ignored: int = 0
    fills_created: int = 0
    exits_created: int = 0
    skipped_existing: int = 0
    unmatched: int = 0
    notes: list[str] = field(default_factory=list)
    dry_run: bool = False

    def summary(self) -> str:
        return (
            f"{self.path}: {self.rows_read} rows ({self.rows_option} option, {self.rows_ignored} ignored); "
            f"fills +{self.fills_created}, exits +{self.exits_created}, already imported {self.skipped_existing}, "
            f"to review in journal/unmatched.json {self.unmatched}"
            + (" [dry-run]" if self.dry_run else "")
        )


def _norm(h: str) -> str:
    return re.sub(r"\s+", " ", h.strip().lower())


def map_headers(headers: list[str], cfg: dict[str, Any]) -> dict[str, str]:
    """logical name -> actual header. Raises RobinhoodFormatError naming exactly what is missing."""
    rcfg = cfg["robinhood"]
    by_norm = {_norm(h): h for h in headers}
    mapping: dict[str, str] = {}
    for logical, aliases in rcfg["columns"].items():
        for a in aliases:
            if _norm(a) in by_norm:
                mapping[logical] = by_norm[_norm(a)]
                break
    missing = [k for k in rcfg["required"] if k not in mapping]
    if missing:
        detail = "; ".join(f"{k} (accepted: {rcfg['columns'][k]})" for k in missing)
        raise RobinhoodFormatError(
            f"CSV is missing required column(s): {detail}. Headers found: {headers}. "
            "Adjust config/journal.yml: robinhood.columns."
        )
    return mapping


def _money(raw: str | None) -> float | None:
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    neg = (s.startswith("(") and s.endswith(")")) or s.startswith("-")
    s = re.sub(r"[()$,\s+-]", "", s)
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def _date(raw: str, formats: list[str]) -> date | None:
    for f in formats:
        try:
            return datetime.strptime(raw.strip(), f).date()
        except ValueError:
            continue
    return None


def parse_description(desc: str) -> tuple[str, str, str, float] | None:
    m = DESC_RE.search(desc[:300])
    if not m:
        return None
    y = int(m["y"])
    y += 2000 if y < 100 else 0
    try:
        exp = date(y, int(m["m"]), int(m["d"])).isoformat()
    except ValueError:
        return None
    return m["und"].upper(), exp, m["right"].lower(), float(m["strike"].replace(",", ""))


def read_rows(path: Path, cfg: dict[str, Any], report: ImportReport) -> list[RhRow]:
    rcfg = cfg["robinhood"]
    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise RobinhoodFormatError("CSV has no header row")
        mp = map_headers(list(reader.fieldnames), cfg)
        raw_rows = list(reader)
    opt_codes = {c.upper() for c in rcfg["option_trans_codes"]}
    out: list[RhRow] = []
    seen: dict[str, int] = defaultdict(int)
    for i, r in enumerate(raw_rows, start=2):
        report.rows_read += 1
        code = (r.get(mp["trans_code"]) or "").strip().upper()
        if code not in opt_codes:
            report.rows_ignored += 1
            continue
        day = _date(r.get(mp["activity_date"]) or "", rcfg["date_formats"])
        parsed = parse_description(r.get(mp["description"]) or "")
        qm = re.match(r"\d+", (r.get(mp["quantity"]) or "").strip())
        if day is None or parsed is None or qm is None:
            report.rows_ignored += 1
            report.notes.append(
                f"line {i}: could not parse date/description/quantity for code {code}; ignored"
            )
            continue
        und, exp, right, strike = parsed
        price = _money(r.get(mp["price"]))
        amount = _money(r.get(mp["amount"])) if "amount" in mp else None
        base = "|".join(
            [
                day.isoformat(),
                code,
                und,
                exp,
                right,
                f"{strike:g}",
                qm.group(),
                str(price),
                str(amount),
            ]
        )
        seen[base] += (
            1  # identical rows in one file (two same-day identical fills) get distinct digests
        )
        digest = hashlib.sha1(f"{base}#{seen[base]}".encode()).hexdigest()[:12]
        out.append(
            RhRow(i, day, und, exp, right, strike, code, int(qm.group()), price, amount, digest)
        )
        report.rows_option += 1
    return out


# --------------------------------------------------------------------------- grouping / classification


def classify(legs: list[dict[str, Any]]) -> str:
    sells = [leg for leg in legs if leg["side"] == "sell"]
    buys = [leg for leg in legs if leg["side"] == "buy"]
    if len(legs) == 1:
        leg = legs[0]
        if leg["side"] == "sell" and leg["type"] == "put":
            return "short_put"
        if leg["side"] == "buy":
            return "long_call" if leg["type"] == "call" else "long_put"
        return "other"
    if (
        len(legs) == 2
        and len(sells) == 1
        and len(buys) == 1
        and sells[0]["type"] == buys[0]["type"]
    ):
        s, b = sells[0], buys[0]
        if s["type"] == "put" and s["strike"] > b["strike"]:
            return "put_credit_spread"
        if s["type"] == "call" and s["strike"] < b["strike"]:
            return "call_credit_spread"
        return "other"
    if len(legs) == 2 and not sells and buys[0]["type"] != buys[1]["type"]:
        return "long_straddle" if buys[0]["strike"] == buys[1]["strike"] else "long_strangle"
    if len(legs) == 4 and len(sells) == 2 and len(buys) == 2:
        sp = [x for x in sells if x["type"] == "put"]
        sc = [x for x in sells if x["type"] == "call"]
        bp = [x for x in buys if x["type"] == "put"]
        bc = [x for x in buys if x["type"] == "call"]
        if (
            len(sp) == len(sc) == len(bp) == len(bc) == 1
            and sp[0]["strike"] > bp[0]["strike"]
            and sc[0]["strike"] < bc[0]["strike"]
        ):
            return "iron_condor"
    return "other"


@dataclass
class Group:
    kind: str  # open | close
    day: date
    und: str
    qty: int
    rows: list[RhRow]

    def legs(self) -> list[dict[str, Any]]:
        out = []
        for r in sorted(self.rows, key=lambda x: (x.expiry, x.right, x.strike, x.code)):
            side = "sell" if r.code in ("STO", "STC") else "buy"
            leg: dict[str, Any] = {
                "side": side,
                "type": r.right,
                "strike": r.strike,
                "expiry": r.expiry,
                "ratio": 1,
            }
            if r.price is not None:
                leg["price"] = r.price
            out.append(leg)
        return out

    def net_price(self) -> float:
        total = 0.0
        for r in self.rows:
            sign = 1.0 if r.code in ("STO", "STC") else -1.0
            total += sign * (r.price or 0.0)
        return round(total, 6)

    def fees(self) -> float:
        if any(
            r.amount is None or r.price is None
            for r in self.rows
            if r.code in ("STO", "BTO", "STC", "BTC")
        ):
            return 0.0
        cash = sum(
            (1.0 if r.code in ("STO", "STC") else -1.0) * (r.price or 0.0) * 100 * r.qty
            for r in self.rows
        )
        amt = sum(r.amount or 0.0 for r in self.rows if r.code in ("STO", "BTO", "STC", "BTC"))
        return round(max(0.0, cash - amt), 4)

    def key(self) -> str:
        return (
            "rh-"
            + hashlib.sha1("|".join(sorted(r.digest for r in self.rows)).encode()).hexdigest()[:12]
        )


def group_rows(rows: list[RhRow], cfg: dict[str, Any]) -> list[Group]:
    rcfg = cfg["robinhood"]
    open_codes = set(rcfg["open_codes"])
    buckets: dict[tuple[str, date, str, int, str], list[RhRow]] = defaultdict(list)
    for r in rows:
        kind = "open" if r.code in open_codes else "close"
        buckets[(kind, r.day, r.und, r.qty, r.expiry)].append(r)
    # merge buckets that differ only by expiry when they form an iron condor etc? Keep strict: one expiry.
    return [
        Group(k[0], k[1], k[2], k[3], v)
        for k, v in sorted(buckets.items(), key=lambda kv: (kv[0][1], kv[0][0], kv[0][2]))
    ]


# ------------------------------------------------------------------------------------- matching


def _leg_sig(legs: list[dict[str, Any]]) -> set[tuple[str, str, float, str]]:
    return {
        (leg["side"], leg["type"], float(leg["strike"]), leg["expiry"])
        for leg in legs
        if leg["type"] != "stock"
    }


def match_alerts(
    group: Group, structure: str, alerts: list[dict[str, Any]], cfg: dict[str, Any]
) -> list[tuple[str, float]]:
    m = cfg["robinhood"]["match"]
    w = m["weights"]
    sig = _leg_sig(group.legs())
    scored: list[tuple[str, float]] = []
    for a in alerts:
        if a["underlying"] != group.und:
            continue
        a_day = datetime.fromisoformat(a["ts_utc"].replace("Z", "+00:00")).astimezone(ET).date()
        delta = (group.day - a_day).days
        if delta < 0 or delta > int(m["window_days"]):
            continue
        a_sig = _leg_sig(a["legs"])
        union = sig | a_sig
        legs_score = len(sig & a_sig) / len(union) if union else 0.0
        if legs_score == 0.0:
            continue
        sug = a.get("contracts_suggested")
        conf = (
            w["legs"] * legs_score
            + w["structure"] * (1.0 if a["structure"] == structure else 0.0)
            + w["time"] * (1.0 - delta / (int(m["window_days"]) + 1))
            + w["size"] * (1.0 if sug is None or group.qty <= sug else 0.0)
        )
        scored.append((a["id"], round(conf, 4)))
    return sorted(scored, key=lambda x: -x[1])


def _ts_for(day: date, cfg: dict[str, Any]) -> str:
    return fmt_ts(et_datetime(day, str(cfg["robinhood"]["assumed_fill_time_et"])))


def unmatched_path(root: Path) -> Path:
    return root / "journal" / "unmatched.json"


def load_unmatched(root: Path) -> list[dict[str, Any]]:
    data = read_json(unmatched_path(root), default=[])
    return data if isinstance(data, list) else []


def import_csv(root: Path | None, path: Path, *, dry_run: bool = False) -> ImportReport:
    root = root or find_root()
    cfg = journal_cfg(root)
    report = ImportReport(path=path.name, dry_run=dry_run)
    rows = read_rows(path, cfg, report)
    groups = group_rows(rows, cfg)
    alerts = store.read_all(root, "alert")
    unmatched = {u["id"]: u for u in load_unmatched(root)}
    new_unmatched: dict[str, dict[str, Any]] = {}

    def park(
        rid: str,
        kind: str,
        reason: str,
        rec: dict[str, Any],
        cands: list[tuple[str, float]] | None = None,
    ) -> None:
        report.unmatched += 1
        new_unmatched[rid] = {
            "id": rid,
            "kind": kind,
            "reason": reason,
            "candidates": [{"alert_id": a, "confidence": c} for a, c in (cands or [])[:3]],
            "proposed_record": rec,
            "source_file": path.name,
        }

    fills = store.read_all(root, "fill")
    exits_ = store.read_all(root, "exit")
    for g in (x for x in groups if x.kind == "open"):
        legs = g.legs()
        structure = classify(legs)
        rid = f"fill-{g.key()}"
        if store.get(root, "fill", rid) is not None:
            report.skipped_existing += 1
            continue
        rec: dict[str, Any] = {
            "id": rid,
            "alert_id": None,
            "ts_utc": _ts_for(g.day, cfg),
            "ts_precision": "date",
            "broker": "robinhood",
            "legs_actual": legs,
            "contracts": g.qty,
            "net_price": g.net_price(),
            "fees_total": g.fees(),
            "source": "csv",
            "meta": {
                "underlying": g.und,
                "structure_guess": structure,
                "csv_lines": [r.line for r in g.rows],
            },
        }
        cands = match_alerts(g, structure, alerts, cfg)
        min_conf = float(cfg["robinhood"]["match"]["min_confidence"])
        if not cands or cands[0][1] < min_conf:
            park(rid, "fill", "no alert matched with enough confidence", rec, cands)
            continue
        if len(cands) > 1 and cands[0][1] - cands[1][1] < 0.05:
            park(rid, "fill", "ambiguous: two alerts match almost equally well", rec, cands)
            continue
        alert_id, conf = cands[0]
        dup = next(
            (
                f
                for f in fills
                if f.get("alert_id") == alert_id
                and f["contracts"] == g.qty
                and datetime.fromisoformat(f["ts_utc"].replace("Z", "+00:00")).astimezone(ET).date()
                == g.day
            ),
            None,
        )
        if dup is not None:
            report.skipped_existing += 1
            report.notes.append(
                f"{rid}: equivalent fill {dup['id']} already logged for {alert_id}; skipped"
            )
            continue
        rec["alert_id"] = alert_id
        rec["meta"]["match_confidence"] = conf
        if not dry_run:
            stored, created = store.append(root, "fill", rec)
            if created:
                fills.append(stored)
        report.fills_created += 1

    open_fills = derive_positions(alerts, fills, exits_)
    for g in (x for x in groups if x.kind == "close"):
        rid = f"exit-{g.key()}"
        if store.get(root, "exit", rid) is not None:
            report.skipped_existing += 1
            continue
        codes = {r.code for r in g.rows}
        reason = (
            "expiry_worthless"
            if codes == {"OEXP"}
            else "assignment"
            if codes & {"OASGN", "OEXC"}
            else "manual"
        )
        ident = {(leg["type"], float(leg["strike"]), leg["expiry"]) for leg in g.legs()}
        target = None
        for p in sorted(open_fills, key=lambda p: p.fill["ts_utc"]):
            f_ident = {
                (leg["type"], float(leg["strike"]), leg["expiry"]) for leg in p.fill["legs_actual"]
            }
            if f_ident == ident and p.contracts_open >= g.qty:
                target = p
                break
        rec = {
            "id": rid,
            "fill_id": target.fill["id"] if target else None,
            "ts_utc": _ts_for(g.day, cfg),
            "ts_precision": "date",
            "contracts": g.qty,
            "net_price": g.net_price(),
            "fees_total": g.fees(),
            "reason": reason,
            "source": "csv",
            "meta": {"underlying": g.und, "csv_lines": [r.line for r in g.rows]},
        }
        if target is None:
            park(rid, "exit", "no open fill with the same legs and enough open contracts", rec)
            continue
        if not dry_run:
            _, created = store.append(root, "exit", rec)
            if created:
                target.exits.append(rec)
        else:
            target.exits.append(rec)
        report.exits_created += 1

    if not dry_run:
        merged = {
            **{k: v for k, v in unmatched.items() if k not in _resolved_ids(root)},
            **new_unmatched,
        }
        # drop entries that have since been journaled
        merged = {k: v for k, v in merged.items() if store.get(root, v["kind"], k) is None}
        write_json(unmatched_path(root), sorted(merged.values(), key=lambda u: u["id"]))
    return report


def _resolved_ids(root: Path) -> set[str]:
    return {r["id"] for kind in ("fill", "exit") for r in store.read_all(root, kind)}


def resolve_unmatched(
    root: Path,
    item_id: str,
    *,
    alert_id: str | None = None,
    fill_id: str | None = None,
    no_alert: bool = False,
) -> dict[str, Any]:
    """Accept a parked item after manual review."""
    items = load_unmatched(root)
    item = next((u for u in items if u["id"] == item_id), None)
    if item is None:
        raise store.JournalError(f"no unmatched item {item_id!r}")
    rec = dict(item["proposed_record"])
    if item["kind"] == "fill":
        if not alert_id and not no_alert:
            raise store.JournalError(
                "give --alert-id, or --no-alert to journal it without an alert link"
            )
        rec["alert_id"] = alert_id
        rec.setdefault("meta", {})["match_confidence"] = "manual"
    else:
        if not fill_id:
            raise store.JournalError("give --fill-id for an unmatched exit")
        rec["fill_id"] = fill_id
    stored, _ = store.append(root, item["kind"], rec)
    write_json(unmatched_path(root), [u for u in items if u["id"] != item_id])
    return stored


__all__ = [
    "ImportReport",
    "RobinhoodFormatError",
    "import_csv",
    "map_headers",
    "parse_description",
    "resolve_unmatched",
    "timedelta",
]
