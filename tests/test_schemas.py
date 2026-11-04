"""Valid and invalid examples for every record type; enums mirrored in config match the schemas."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from odj.common.config import journal_cfg
from odj.common.schema import ValidationFailed, validate
from tests.helpers import alert, exit_, fill

REPO = Path(__file__).resolve().parents[1]


def skip_rec(**over: Any) -> dict[str, Any]:
    s: dict[str, Any] = {
        "id": "skip-1",
        "alert_id": alert()["id"],
        "ts_utc": "2026-11-02T15:00:00Z",
        "reason": "did_not_see",
    }
    s.update(over)
    return s


def test_valid_examples() -> None:
    assert validate("alert", alert())["id"] == alert()["id"]
    assert validate("fill", fill())["broker"] == "robinhood"  # default filled
    assert validate("exit", exit_())["reason"] == "profit_target"
    assert validate("skip", skip_rec())["reason"] == "did_not_see"


def test_alert_minimal_and_nulls_allowed() -> None:
    a = alert(
        confidence_score=None,
        regime_gate_state=None,
        max_loss=None,
        max_profit=None,
        contracts_suggested=None,
        notes=None,
    )
    assert validate("alert", a)["confidence_score"] is None


def test_timestamps_are_normalized_to_utc_z() -> None:
    a = validate("alert", alert(ts_utc="2026-11-02T09:35:00-05:00"))
    assert a["ts_utc"] == "2026-11-02T14:35:00Z"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda a: a.update(structure="iron_butterfly"),
        lambda a: a.update(legs=[]),
        lambda a: a.pop("legs"),
        lambda a: a.update(alert_price_basis="vibes"),
        lambda a: a.update(confidence_score=0),
        lambda a: a.update(confidence_score=101),
        lambda a: a.update(id="not-an-alert-id"),
        lambda a: a.update(ts_utc="2026-11-02 14:35"),
        lambda a: a.update(ts_utc="not a time"),
        lambda a: a.update(underlying="spy; rm -rf"),
        lambda a: a.update(alert_net_price="0.9"),
        lambda a: a.update(unexpected_field=1),
        lambda a: a["legs"][0].pop("strike"),
        lambda a: a["legs"][0].update(side="hold"),
        lambda a: a["legs"][0].update(expiry="11/20/2026"),
        lambda a: a.update(max_loss=-5),
        lambda a: a.update(id="other-bot-20261102T143500Z-1"),  # id prefix must equal bot
        lambda a: a["exit_rules"].update(profit_target_pct_of_credit=-1),
    ],
)
def test_invalid_alerts_rejected(mutate: Any) -> None:
    a = copy.deepcopy(alert())
    mutate(a)
    with pytest.raises(ValidationFailed):
        validate("alert", a)


def test_option_structure_requires_option_legs() -> None:
    a = alert(structure="put_credit_spread", legs=[{"side": "buy", "type": "stock"}])
    with pytest.raises(ValidationFailed, match="option leg"):
        validate("alert", a)
    ok = alert(structure="equity_long", legs=[{"side": "buy", "type": "stock"}])
    assert validate("alert", ok)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda f: f.update(contracts=0),
        lambda f: f.update(net_price="x"),
        lambda f: f.update(order_type="stop"),
        lambda f: f.update(source="telepathy"),
        lambda f: f.update(fees_total=-1),
        lambda f: f.update(legs_actual=[]),
        lambda f: f.pop("net_price"),
        lambda f: f.update(id="a b"),
    ],
)
def test_invalid_fills_rejected(mutate: Any) -> None:
    f = copy.deepcopy(fill())
    mutate(f)
    with pytest.raises(ValidationFailed):
        validate("fill", f)


@pytest.mark.parametrize(
    ("mutate", "kind"),
    [
        (lambda e: e.update(reason="rage_quit"), "exit"),
        (lambda e: e.update(contracts=-2), "exit"),
        (lambda e: e.pop("fill_id"), "exit"),
        (lambda e: e.update(reason="vibes"), "skip"),
        (lambda e: e.pop("alert_id"), "skip"),
        (lambda e: e.update(notes="x" * 2001), "skip"),
    ],
)
def test_invalid_exit_and_skip_rejected(mutate: Any, kind: str) -> None:
    rec = copy.deepcopy(exit_() if kind == "exit" else skip_rec())
    mutate(rec)
    with pytest.raises(ValidationFailed):
        validate(kind, rec)


def test_config_enums_mirror_schema_enums() -> None:
    enums = journal_cfg()["enums"]

    def sch(name: str) -> dict[str, Any]:
        return json.loads((REPO / "schemas" / f"{name}.schema.json").read_text())

    assert set(enums["structure"]) == set(sch("alert")["properties"]["structure"]["enum"])
    assert set(enums["alert_price_basis"]) == set(
        sch("alert")["properties"]["alert_price_basis"]["enum"]
    )
    assert set(enums["order_type"]) == set(sch("fill")["properties"]["order_type"]["enum"])
    assert set(enums["source"]) == set(sch("fill")["properties"]["source"]["enum"])
    assert set(enums["exit_reason"]) == set(sch("exit")["properties"]["reason"]["enum"])
    assert set(enums["skip_reason"]) == set(sch("skip")["properties"]["reason"]["enum"])
