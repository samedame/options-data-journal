"""Shadow tracker: mechanical replay of exit rules on snapshot marks (pure functions)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import pytest

from odj.common.timeutil import et_datetime
from odj.journal.shadow import (
    Quote,
    SnapView,
    close_cashflow,
    contract_key,
    settlement_cashflow,
    track_alert,
)
from tests.helpers import alert

EXP = "2026-11-20"
SHORT = contract_key(EXP, 530.0, "P")
LONG = contract_key(EXP, 525.0, "P")
ENTRY_CREDIT = 1.00


def snap(
    d: str,
    slot: str,
    spot: float,
    short: tuple[float, float] | None,
    long: tuple[float, float] | None,
) -> SnapView:
    day = date.fromisoformat(d)
    hhmm = "09:45" if slot == "open" else "15:50"
    q: dict[Any, Quote] = {}
    if short is not None:
        q[SHORT] = Quote(*short)
    if long is not None:
        q[LONG] = Quote(*long)
    return SnapView(et_datetime(day, hhmm).astimezone(UTC), slot, spot, q)


def a(**rules: Any) -> dict[str, Any]:
    return alert(
        ts_utc="2026-11-02T14:35:00Z",
        alert_net_price=ENTRY_CREDIT,
        exit_rules=rules,
        max_loss=400.0,
        legs=[
            {"side": "sell", "type": "put", "strike": 530.0, "expiry": EXP, "ratio": 1},
            {"side": "buy", "type": "put", "strike": 525.0, "expiry": EXP, "ratio": 1},
        ],
    )


# healthy, unthreatening marks: short 1.00 / long 0.40 mid -> cost 0.60 -> pnl +0.40 (40% of credit)
CALM = ((0.95, 1.05), (0.35, 0.45))


def test_close_cashflow_mid_vs_conservative() -> None:
    legs = a()["legs"]
    qs = {SHORT: Quote(0.20, 0.40), LONG: Quote(0.04, 0.12)}
    assert close_cashflow(legs, qs, "mid") == pytest.approx(-(0.30) + 0.08)
    assert close_cashflow(legs, qs, "conservative") == pytest.approx(
        -0.40 + 0.04
    )  # buy short at ask, sell long at bid
    assert close_cashflow(legs, {SHORT: Quote(0.2, 0.4)}, "mid") is None  # missing leg
    assert (
        close_cashflow(legs, {SHORT: Quote(0.2, 0.0), LONG: Quote(0.04, 0.12)}, "mid") is None
    )  # ask 0 = invalid
    assert (
        close_cashflow(legs, {SHORT: Quote(0.5, 0.4), LONG: Quote(0.04, 0.12)}, "mid") is None
    )  # crossed


def test_settlement_cashflow() -> None:
    legs = a()["legs"]
    assert settlement_cashflow(legs, 540.0) == 0.0
    assert settlement_cashflow(legs, 520.0) == pytest.approx(-10.0 + 5.0)
    assert settlement_cashflow(legs, 527.0) == pytest.approx(-3.0)


def test_profit_target_hit() -> None:
    snaps = [
        snap("2026-11-03", "open", 541, *CALM),
        snap("2026-11-04", "open", 542, (0.25, 0.35), (0.05, 0.15)),
    ]
    r = track_alert(a(profit_target_pct_of_credit=50, stop_loss_multiple_of_credit=2), snaps)
    assert r.status == "closed" and r.exit_reason == "profit_target"
    assert r.theoretical_exit_price == pytest.approx(-0.20)  # 0.30 - 0.10 to close
    assert r.theoretical_pnl == pytest.approx(80.0)
    assert r.theoretical_exit_ts == "2026-11-04T14:45:00Z"
    assert r.n_marks == 2


def test_stop_hit() -> None:
    snaps = [
        snap("2026-11-03", "open", 541, *CALM),
        snap("2026-11-04", "close", 526, (4.15, 4.25), (0.95, 1.05)),
    ]
    r = track_alert(a(profit_target_pct_of_credit=50, stop_loss_multiple_of_credit=2), snaps)
    # cost to close 4.20 - 1.00 = 3.20 -> pnl 1.00 - 3.20 = -2.20 <= -2.0 * credit
    assert r.exit_reason == "stop" and r.theoretical_pnl == pytest.approx(-220.0)


def test_dte_exit() -> None:
    snaps = [
        snap("2026-11-03", "open", 541, *CALM),
        snap("2026-11-13", "open", 541, *CALM),
        snap("2026-11-16", "open", 541, *CALM),
    ]
    r = track_alert(a(profit_target_pct_of_credit=80, dte_exit=5), snaps)
    assert r.exit_reason == "dte"
    assert (
        r.theoretical_exit_ts == "2026-11-16T14:45:00Z"
    )  # first snapshot with <= 5 days to 11-20 (11-16 is 4d; 11-13 is 7d)
    assert r.theoretical_pnl == pytest.approx(40.0)


def test_time_exit_days_and_iso() -> None:
    snaps = [
        snap("2026-11-03", "open", 541, *CALM),
        snap("2026-11-05", "open", 541, *CALM),
        snap("2026-11-06", "open", 541, *CALM),
    ]
    assert (
        track_alert(a(time_exit=3), snaps).theoretical_exit_ts == "2026-11-05T14:45:00Z"
    )  # alert 11-02 14:35Z + 3d = 11-05 14:35Z
    assert (
        track_alert(a(time_exit="2026-11-05T00:00:00Z"), snaps).theoretical_exit_ts
        == "2026-11-05T14:45:00Z"
    )
    assert track_alert(a(time_exit=3), snaps).exit_reason == "time"


def test_expiry_worthless() -> None:
    snaps = [
        snap("2026-11-03", "open", 541, *CALM),
        snap("2026-11-20", "close", 541.0, (0.0, 0.01), (0.0, 0.01)),
    ]
    r = track_alert(a(), snaps)
    assert r.exit_reason == "expiry_worthless" and r.theoretical_pnl == pytest.approx(100.0)
    assert r.theoretical_exit_price == 0.0
    assert any("proxy" in n for n in r.notes)


def test_expiry_max_loss() -> None:
    snaps = [
        snap("2026-11-03", "open", 541, *CALM),
        snap("2026-11-20", "close", 520.0, (10.0, 10.1), (5.0, 5.1)),
    ]
    r = track_alert(a(), snaps)  # no stop rule: ride to expiry
    assert r.exit_reason == "expiry_max_loss"
    assert r.theoretical_pnl == pytest.approx(-400.0)


def test_expiry_partial_itm_is_settled_not_max_loss() -> None:
    snaps = [
        snap("2026-11-03", "open", 541, *CALM),
        snap("2026-11-20", "close", 527.0, (3.0, 3.1), (0.0, 0.01)),
    ]
    r = track_alert(a(), snaps)
    assert r.exit_reason == "expiry_settled" and r.theoretical_pnl == pytest.approx(
        (1.0 - 3.0) * 100
    )


def test_unmarkable_when_a_leg_is_never_in_the_snapshots() -> None:
    snaps = [
        snap("2026-11-03", "open", 541, (0.9, 1.0), None),
        snap("2026-11-04", "open", 541, (0.9, 1.0), None),
    ]
    r = track_alert(a(profit_target_pct_of_credit=50), snaps)
    assert (
        r.status == "unmarkable"
        and "never present" in (r.reason or "")
        and "525" in (r.reason or "")
    )
    assert r.theoretical_pnl is None


def test_unmarkable_for_stock_leg_multi_expiry_and_zero_price() -> None:
    stock = alert(
        structure="equity_long", legs=[{"side": "buy", "type": "stock"}], alert_net_price=-100.0
    )
    assert track_alert(stock, []).status == "unmarkable"
    multi = a()
    multi["legs"][1]["expiry"] = "2026-12-18"
    assert "multi-expiry" in (track_alert(multi, []).reason or "")
    zero = a()
    zero["alert_net_price"] = 0.0
    assert track_alert(zero, []).status == "unmarkable"


def test_unmarkable_when_expiry_passed_without_settlement_snapshot() -> None:
    snaps = [snap("2026-11-03", "open", 541, *CALM), snap("2026-11-23", "open", 541, None, None)]
    snaps[1].quotes[SHORT] = Quote(0.0, 0.01)
    snaps[1].quotes[LONG] = Quote(0.0, 0.01)
    r = track_alert(a(), snaps)
    assert r.status == "unmarkable" and "cannot settle" in (r.reason or "")


def test_mid_vs_conservative_mark_modes_can_disagree() -> None:
    wide = [snap("2026-11-03", "open", 541, (0.20, 0.40), (0.04, 0.12))]
    # mid: pnl 1.00-0.22 = 0.78 (>=70%) -> target; conservative: 1.00-0.36 = 0.64 (<70%) -> still open
    rules = {"profit_target_pct_of_credit": 70}
    assert track_alert(a(**rules), wide, "mid").exit_reason == "profit_target"
    cons = track_alert(a(**rules), wide, "conservative")
    assert cons.status == "open" and cons.unrealized_pnl == pytest.approx(64.0)


def test_target_touched_only_intraday_is_a_documented_known_miss() -> None:
    """The spread may have hit the 50% target at, say, 11:30 and bounced back by 15:50, but twice-daily
    snapshots never see it. The tracker must NOT detect it (no interpolation) - this pins that behaviour."""
    snaps = [
        snap("2026-11-03", "open", 541, (0.95, 1.05), (0.35, 0.45)),  # +40%
        snap(
            "2026-11-03", "close", 541, (0.90, 1.00), (0.30, 0.40)
        ),  # +35%  (intraday low of cost ~0.45 unseen)
    ]
    r = track_alert(a(profit_target_pct_of_credit=50), snaps)
    assert r.status == "open" and r.exit_reason is None and r.n_marks == 2


def test_invalid_quotes_are_gaps_not_interpolated() -> None:
    snaps = [
        snap("2026-11-03", "open", 541, (0.5, 0.0), (0.05, 0.15)),  # ask 0: unusable
        snap("2026-11-04", "open", 541, (0.25, 0.35), (0.05, 0.15)),
    ]
    r = track_alert(a(profit_target_pct_of_credit=50), snaps)
    assert r.exit_reason == "profit_target" and r.n_gap_snapshots == 1 and r.n_marks == 1
    assert r.theoretical_exit_ts == "2026-11-04T14:45:00Z"


def test_snapshots_before_the_alert_are_ignored() -> None:
    early = SnapView(
        datetime(2026, 11, 2, 13, 0, tzinfo=UTC),
        "open",
        541,
        {SHORT: Quote(0.2, 0.3), LONG: Quote(0.04, 0.1)},
    )
    r = track_alert(a(profit_target_pct_of_credit=50), [early])
    assert r.status == "unmarkable"  # no later snapshot carries the legs


def test_stop_beats_profit_target_priority_and_earliest_snapshot_wins() -> None:
    snaps = [
        snap("2026-11-03", "open", 541, (0.25, 0.35), (0.05, 0.15)),
        snap("2026-11-04", "open", 520, (4.15, 4.25), (0.95, 1.05)),
    ]
    r = track_alert(a(profit_target_pct_of_credit=50, stop_loss_multiple_of_credit=2), snaps)
    assert r.exit_reason == "profit_target" and r.theoretical_exit_ts == "2026-11-03T14:45:00Z"
