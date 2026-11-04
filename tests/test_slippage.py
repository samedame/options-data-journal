"""Hand-computed slippage / P&L cases. Convention: per share, credit +, debit -. Negative slippage = worse."""

from __future__ import annotations

import pytest

from odj.journal.positions import derive_positions
from odj.journal.slippage import (
    entry_slippage,
    exit_slippage,
    multiplier_for_legs,
    realized_pnl_usd,
)
from tests.helpers import alert, exit_, fill


def test_credit_spread_partial_fill_worse_than_alert() -> None:
    # alert: credit 0.50, max loss $450/contract. Filled at 0.42 -> gave up 8c.
    s = entry_slippage(0.50, 0.42, max_loss_usd=450.0)
    assert s["per_share"] == pytest.approx(-0.08)
    assert s["usd_per_contract"] == pytest.approx(-8.0)
    assert s["pct_of_alert_premium"] == pytest.approx(-16.0)
    assert s["pct_of_max_loss"] == pytest.approx(-8.0 / 450.0 * 100, abs=1e-3)


def test_credit_spread_better_than_alert_is_positive() -> None:
    s = entry_slippage(0.50, 0.55)
    assert s["per_share"] == pytest.approx(0.05) and s["usd_per_contract"] == pytest.approx(5.0)
    assert s["pct_of_max_loss"] is None


def test_debit_straddle_paying_more_is_negative() -> None:
    # alert: pay 4.00 (-4.00). Filled paying 4.10 (-4.10) -> worse by 10c.
    s = entry_slippage(-4.00, -4.10, max_loss_usd=400.0)
    assert s["per_share"] == pytest.approx(-0.10)
    assert s["usd_per_contract"] == pytest.approx(-10.0)
    assert s["pct_of_alert_premium"] == pytest.approx(-2.5)
    assert s["pct_of_max_loss"] == pytest.approx(-2.5)


def test_debit_paying_less_is_positive() -> None:
    assert entry_slippage(-4.00, -3.90)["per_share"] == pytest.approx(0.10)


def test_iron_condor() -> None:
    s = entry_slippage(1.20, 1.05, max_loss_usd=380.0)
    assert s["per_share"] == pytest.approx(-0.15)
    assert s["usd_per_contract"] == pytest.approx(-15.0)
    assert s["pct_of_alert_premium"] == pytest.approx(-12.5)
    assert s["pct_of_max_loss"] == pytest.approx(-15.0 / 380.0 * 100, abs=1e-3)


def test_equity_multiplier_is_one() -> None:
    legs = [{"side": "buy", "type": "stock"}]
    assert multiplier_for_legs(legs) == 1
    assert (
        multiplier_for_legs([{"side": "sell", "type": "put", "strike": 1, "expiry": "2026-01-01"}])
        == 100
    )
    assert entry_slippage(-100.0, -100.2, multiplier=1)["usd_per_contract"] == pytest.approx(-0.2)


def test_zero_alert_price_gives_no_percentage() -> None:
    assert entry_slippage(0.0, -0.05)["pct_of_alert_premium"] is None


def test_exit_slippage_signs() -> None:
    # shadow says the target triggered at a close cost of 0.20 (net -0.20); I paid 0.25 (net -0.25): worse
    worse = exit_slippage(-0.25, -0.20)
    assert worse["per_share"] == pytest.approx(-0.05) and worse[
        "usd_per_contract"
    ] == pytest.approx(-5.0)
    better = exit_slippage(-0.15, -0.20)
    assert better["per_share"] == pytest.approx(0.05)
    assert exit_slippage(0.0, 0.0)["usd_per_contract"] == pytest.approx(
        0.0
    )  # expired worthless both ways


def test_realized_pnl_after_fees() -> None:
    # entry credit 0.50, close for 0.20 (net -0.20), 2 contracts -> 0.30*100*2 = 60, minus 1.30 + 1.30
    assert realized_pnl_usd(0.50, -0.20, 2, entry_fees_alloc=1.30, exit_fees=1.30) == pytest.approx(
        57.40
    )


def test_position_partial_exits_allocate_entry_fees_pro_rata() -> None:
    f = fill(contracts=4, net_price=0.50, fees_total=4.0)
    e1 = exit_(id="e1", contracts=1, net_price=-0.20, fees_total=0.5, ts_utc="2026-11-05T15:00:00Z")
    e2 = exit_(id="e2", contracts=2, net_price=-0.10, fees_total=1.0, ts_utc="2026-11-06T15:00:00Z")
    (p,) = derive_positions([alert()], [f], [e1, e2])
    assert p.contracts_open == 1 and not p.is_closed
    # e1: (0.50-0.20)*100*1 - 1.0 (1/4 of 4.0) - 0.5 = 28.5 ; e2: (0.50-0.10)*100*2 - 2.0 - 1.0 = 77.0
    assert p.realized_pnl_usd() == pytest.approx(28.5 + 77.0)
    assert p.realized_pnl_per_contract() == pytest.approx((28.5 + 77.0) / 3)
    p2 = derive_positions(
        [alert()], [f], [e1, e2, exit_(id="e3", contracts=1, net_price=0.0, fees_total=0.0)]
    )[0]
    assert p2.is_closed
