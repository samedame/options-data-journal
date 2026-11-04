"""Robinhood CSV import against a SYNTHETIC export (column names / trans codes are a hypothesis)."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from odj.common.config import journal_cfg
from odj.journal import store
from odj.journal.robinhood import (
    ImportReport,
    RobinhoodFormatError,
    classify,
    import_csv,
    load_unmatched,
    map_headers,
    parse_description,
    resolve_unmatched,
)
from tests.helpers import FIXTURES, alert, fill

CSV = FIXTURES / "robinhood_activity_SYNTHETIC.csv"
A1 = alert()  # SPY 530/525 put credit spread exp 2026-11-20, 2026-11-02, net 0.90, suggested 2
A2 = alert(
    id="spy-bps-bot-20261109T144000Z-1",
    ts_utc="2026-11-09T14:40:00Z",
    alert_net_price=0.55,
    contracts_suggested=1,
    legs=[
        {"side": "sell", "type": "put", "strike": 520.0, "expiry": "2026-11-27", "ratio": 1},
        {"side": "buy", "type": "put", "strike": 515.0, "expiry": "2026-11-27", "ratio": 1},
    ],
)


def digest(root: Path) -> str:
    h = hashlib.sha256()
    for f in sorted((root / "journal").rglob("*")):
        if f.is_file() and not f.name.endswith(".lock"):
            h.update(str(f.relative_to(root)).encode())
            h.update(f.read_bytes())
    return h.hexdigest()


@pytest.fixture
def seeded(root: Path) -> Path:
    store.append(root, "alert", A1)
    store.append(root, "alert", A2)
    return root


def test_full_import_matches_groups_and_parks_the_rest(seeded: Path) -> None:
    rep = import_csv(seeded, CSV)
    assert (rep.rows_read, rep.rows_option, rep.rows_ignored) == (
        12,
        10,
        2,
    )  # INT and ACH rows are not option activity
    assert (rep.fills_created, rep.exits_created, rep.unmatched) == (2, 2, 1)
    fills = {f["alert_id"]: f for f in store.read_all(seeded, "fill")}
    f1, f2 = fills[A1["id"]], fills[A2["id"]]
    assert (
        f1["id"].startswith("fill-rh-") and f1["source"] == "csv" and f1["ts_precision"] == "date"
    )
    assert f1["contracts"] == 2 and f1["net_price"] == pytest.approx(0.90)
    assert f1["fees_total"] == pytest.approx(
        0.06
    )  # (1.40-0.50)*200 = 180.00 cash vs 179.94 amounts
    assert {(leg["side"], leg["strike"]) for leg in f1["legs_actual"]} == {
        ("sell", 530.0),
        ("buy", 525.0),
    }
    assert f1["meta"]["match_confidence"] >= 0.8
    exits = {e["fill_id"]: e for e in store.read_all(seeded, "exit")}
    assert (
        exits[f1["id"]]["net_price"] == pytest.approx(-0.20)
        and exits[f1["id"]]["reason"] == "manual"
    )
    assert exits[f1["id"]]["contracts"] == 2 and exits[f1["id"]]["fees_total"] == pytest.approx(
        0.04
    )
    assert exits[f2["id"]]["reason"] == "expiry_worthless" and exits[f2["id"]]["net_price"] == 0.0
    (u,) = load_unmatched(seeded)
    assert u["kind"] == "fill" and "no alert matched" in u["reason"]
    assert u["proposed_record"]["meta"]["structure_guess"] == "call_credit_spread"


def test_reimporting_the_same_csv_changes_nothing(seeded: Path) -> None:
    import_csv(seeded, CSV)
    before = digest(seeded)
    again = import_csv(seeded, CSV)
    assert digest(seeded) == before
    assert again.fills_created == 0 and again.exits_created == 0 and again.skipped_existing >= 4


def test_dry_run_writes_nothing(seeded: Path) -> None:
    before = digest(seeded)
    rep = import_csv(seeded, CSV, dry_run=True)
    assert rep.dry_run and rep.fills_created == 2 and digest(seeded) == before


def test_missing_required_column_gives_a_precise_error(root: Path, tmp_path: Path) -> None:
    bad = tmp_path / "bad.csv"
    bad.write_text(CSV.read_text().replace("Trans Code", "Transaction Type"))
    with pytest.raises(RobinhoodFormatError) as ei:
        import_csv(root, bad)
    msg = str(ei.value)
    assert (
        "trans_code" in msg and "Trans Code" in msg and "Transaction Type" in msg
    )  # names the gap and what was found


def test_header_aliases_and_case_are_tolerated() -> None:
    mp = map_headers(
        ["activity date", "SYMBOL", "description", "TRANSACTION CODE", "Qty", "price", "Amount"],
        journal_cfg(),
    )
    assert (
        mp["activity_date"] == "activity date"
        and mp["trans_code"] == "TRANSACTION CODE"
        and mp["quantity"] == "Qty"
    )


def test_alias_headers_import_end_to_end(seeded: Path, tmp_path: Path) -> None:
    alt = tmp_path / "alt.csv"
    alt.write_text(CSV.read_text().replace("Activity Date", "Date").replace("Quantity", "Qty"))
    assert import_csv(seeded, alt).fills_created == 2


def test_parse_description() -> None:
    assert parse_description("SPY 11/20/2026 Put $530.00") == ("SPY", "2026-11-20", "put", 530.0)
    assert parse_description("QQQ 1/2/26 Call $1,050.50") == ("QQQ", "2026-01-02", "call", 1050.5)
    assert parse_description("Dividend from SPY") is None
    assert parse_description("SPY 13/45/2026 Put $5") is None


def leg(side: str, typ: str, strike: float) -> dict[str, object]:
    return {"side": side, "type": typ, "strike": strike}


def test_classification() -> None:
    assert classify([leg("sell", "put", 530), leg("buy", "put", 525)]) == "put_credit_spread"
    assert (
        classify([leg("sell", "put", 525), leg("buy", "put", 530)]) == "other"
    )  # that is a debit spread
    assert classify([leg("sell", "call", 540), leg("buy", "call", 545)]) == "call_credit_spread"
    assert (
        classify(
            [
                leg("sell", "put", 530),
                leg("buy", "put", 525),
                leg("sell", "call", 550),
                leg("buy", "call", 555),
            ]
        )
        == "iron_condor"
    )
    assert classify([leg("buy", "call", 540), leg("buy", "put", 540)]) == "long_straddle"
    assert classify([leg("buy", "call", 550), leg("buy", "put", 530)]) == "long_strangle"
    assert classify([leg("sell", "put", 530)]) == "short_put"
    assert classify([leg("buy", "call", 540)]) == "long_call"
    assert classify([leg("sell", "call", 540)]) == "other"


def test_low_confidence_is_never_forced(root: Path) -> None:
    near = alert(
        legs=[
            {"side": "sell", "type": "put", "strike": 530.0, "expiry": "2026-11-20", "ratio": 1},
            {"side": "buy", "type": "put", "strike": 520.0, "expiry": "2026-11-20", "ratio": 1},
        ]
    )
    store.append(root, "alert", near)  # similar but not the same legs (520 instead of 525)
    rep = import_csv(root, CSV)
    assert rep.fills_created == 0 and store.read_all(root, "fill") == []
    assert any(u["candidates"] for u in load_unmatched(root))


def test_ambiguous_alerts_are_parked(root: Path) -> None:
    store.append(root, "alert", A1)
    twin = alert(id="spy-bps-bot-20261102T143501Z-2", ts_utc="2026-11-02T14:35:01Z")
    store.append(root, "alert", twin)
    import_csv(root, CSV)
    assert store.read_all(root, "fill") == []
    assert any("ambiguous" in u["reason"] for u in load_unmatched(root))


def test_alert_outside_the_time_window_does_not_match(root: Path) -> None:
    store.append(
        root, "alert", alert(ts_utc="2026-10-20T14:35:00Z", id="spy-bps-bot-20261020T143500Z-1")
    )
    import_csv(root, CSV)
    assert store.read_all(root, "fill") == []


def test_an_already_logged_manual_fill_is_not_duplicated(seeded: Path) -> None:
    store.append(seeded, "fill", fill(ts_utc="2026-11-02T14:40:00Z"))  # logged from the phone
    rep = import_csv(seeded, CSV)
    assert len([f for f in store.read_all(seeded, "fill") if f["alert_id"] == A1["id"]]) == 1
    assert any("already logged" in n for n in rep.notes)


def test_resolving_an_unmatched_item(seeded: Path) -> None:
    import_csv(seeded, CSV)
    (u,) = load_unmatched(seeded)
    with pytest.raises(store.JournalError):
        resolve_unmatched(seeded, u["id"])  # must say what to do with it
    stored = resolve_unmatched(seeded, u["id"], no_alert=True)
    assert stored["alert_id"] is None and load_unmatched(seeded) == []
    again = import_csv(seeded, CSV)  # and a re-import does not resurrect it
    assert load_unmatched(seeded) == [] and again.fills_created == 0


def test_a_close_without_an_open_fill_is_parked(root: Path, tmp_path: Path) -> None:
    lines = CSV.read_text().splitlines()
    closes = tmp_path / "closes.csv"
    closes.write_text(
        "\n".join([lines[0], *[ln for ln in lines if ",BTC," in ln or ",STC," in ln]]) + "\n"
    )
    rep = import_csv(root, closes)
    assert rep.exits_created == 0 and rep.unmatched == 1
    assert load_unmatched(root)[0]["kind"] == "exit"


def test_report_summary_text() -> None:
    assert "dry-run" in ImportReport(path="x.csv", dry_run=True).summary()


def test_fixture_copy_survives_tmp(tmp_path: Path) -> None:
    shutil.copy(CSV, tmp_path / "a.csv")
    assert (
        (tmp_path / "a.csv")
        .read_text()
        .startswith("Activity Date,Process Date,Settle Date,Instrument")
    )
