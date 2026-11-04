"""Bot-side client (never raises, dry-run, payload shape) and the staleness / watchdog logic."""

from __future__ import annotations

import io
import json
import urllib.error
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from odj.common import heartbeat
from odj.common.staleness import overdue
from odj.common.timeutil import et_datetime
from odj.journal import client
from odj.journal.watchdog import check, weekly_expected
from tests.helpers import alert, utc

# ------------------------------------------------------------------------------ client


class FakeResp:
    status = 204

    def __enter__(self) -> FakeResp:
        return self

    def __exit__(self, *a: object) -> None:
        return None


def test_payload_is_one_json_string_property(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    def fake_urlopen(req: Any, timeout: float = 0) -> FakeResp:
        seen["url"], seen["body"], seen["headers"], seen["timeout"] = (
            req.full_url,
            json.loads(req.data),
            dict(req.header_items()),
            timeout,
        )
        return FakeResp()

    monkeypatch.setattr(client.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("JOURNAL_REPO", "samedame/options-data-journal")
    monkeypatch.setenv("JOURNAL_DISPATCH_TOKEN", "ghp_SECRET")
    assert client.send_alert(alert()) is True
    assert seen["url"] == "https://api.github.com/repos/samedame/options-data-journal/dispatches"
    assert seen["body"]["event_type"] == "alert"
    assert list(seen["body"]["client_payload"]) == [
        "alert"
    ]  # 1 top-level property (GitHub caps at 10)
    assert isinstance(seen["body"]["client_payload"]["alert"], str)
    assert json.loads(seen["body"]["client_payload"]["alert"])["id"] == alert()["id"]
    assert seen["timeout"] <= 10


@pytest.mark.parametrize(
    "exc",
    [
        urllib.error.URLError("down"),
        TimeoutError(),
        ConnectionResetError(),
        ValueError("x"),
        RuntimeError("y"),
    ],
)
def test_never_raises_into_the_host_bot(
    monkeypatch: pytest.MonkeyPatch, exc: Exception, capsys: pytest.CaptureFixture[str]
) -> None:
    def boom(*a: Any, **k: Any) -> None:
        raise exc

    monkeypatch.setattr(client.urllib.request, "urlopen", boom)
    monkeypatch.setenv("JOURNAL_REPO", "o/r")
    monkeypatch.setenv("JOURNAL_DISPATCH_TOKEN", "ghp_SECRET")
    assert client.send_alert(alert()) is False
    assert "ghp_SECRET" not in capsys.readouterr().err  # token never logged


def test_http_error_returns_false_without_leaking_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def denied(*a: Any, **k: Any) -> None:
        raise urllib.error.HTTPError("u", 403, "forbidden", {}, io.BytesIO(b"nope"))  # type: ignore[arg-type]

    monkeypatch.setattr(client.urllib.request, "urlopen", denied)
    monkeypatch.setenv("JOURNAL_REPO", "o/r")
    monkeypatch.setenv("JOURNAL_DISPATCH_TOKEN", "ghp_SECRET")
    assert client.send_alert(alert()) is False
    err = capsys.readouterr().err
    assert "403" in err and "ghp_SECRET" not in err


def test_missing_env_and_invalid_alert_return_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JOURNAL_REPO", raising=False)
    monkeypatch.delenv("JOURNAL_DISPATCH_TOKEN", raising=False)
    assert client.send_alert(alert()) is False
    monkeypatch.setenv("JOURNAL_REPO", "o/r")
    monkeypatch.setenv("JOURNAL_DISPATCH_TOKEN", "t")
    assert client.send_alert({"nope": 1}) is False
    assert client.send_alert(None) is False  # type: ignore[arg-type]


def test_dry_run_validates_prints_and_never_sends(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def forbidden(*a: Any, **k: Any) -> None:
        raise AssertionError("network used in dry run")

    monkeypatch.setattr(client.urllib.request, "urlopen", forbidden)
    assert client.send_alert(alert(), dry_run=True) is True
    assert alert()["id"] in capsys.readouterr().out
    monkeypatch.setenv("ODJ_DRY_RUN", "1")
    assert client.send_alert(alert()) is True
    assert client.send_alert({"bad": 1}, dry_run=True) is False


def test_client_enums_match_the_schemas() -> None:
    schema = json.loads(
        (Path(__file__).resolve().parents[1] / "schemas/alert.schema.json").read_text()
    )
    assert set(schema["properties"]["structure"]["enum"]) == client.STRUCTURES
    assert set(schema["properties"]["alert_price_basis"]["enum"]) == client.PRICE_BASIS


def test_client_is_stdlib_only_and_make_alert_id_roundtrips() -> None:
    src = (Path(client.__file__)).read_text()
    imports = {
        ln.split()[1].split(".")[0]
        for ln in src.splitlines()
        if ln.startswith(("import ", "from ")) and "__future__" not in ln
    }
    assert imports <= {"json", "os", "re", "sys", "urllib", "datetime", "typing"}
    aid = client.make_alert_id("tier1-bot", 3, datetime(2026, 11, 2, 14, 35, tzinfo=UTC))
    assert aid == "tier1-bot-20261102T143500Z-3" and not client.validate_light(
        {**alert(bot="tier1-bot"), "id": aid}
    )


# ------------------------------------------------------------------------------ staleness


def t(h: int, m: int = 0, d: int = 3) -> datetime:
    return datetime(2026, 11, d, h, m, tzinfo=UTC)


def test_overdue_generic() -> None:
    exp = [t(14, 45), t(20, 50)]
    grace = timedelta(minutes=45)
    assert overdue(exp, [t(14, 47), t(20, 52)], t(22), grace) == []
    assert overdue(exp, [t(14, 47)], t(22), grace) == [t(20, 50)]
    assert overdue(exp, [], t(14, 50), grace) == []  # still inside grace
    assert overdue(exp, [], t(15, 31), grace) == [t(14, 45)]
    assert overdue(exp, [t(20, 52)], t(22), grace) == [
        t(14, 45)
    ]  # a later success cannot excuse an earlier miss
    assert (
        overdue([t(14, 45)], [t(14, 44)], t(16), grace) == []
    )  # runs a minute early count (tolerance)


# ------------------------------------------------------------------------------ watchdog


def hb_ok(root: Path, d: date, slot: str, hhmm: str) -> None:
    heartbeat.record(
        root,
        "snapshot",
        status="ok",
        slot_key=f"{d.isoformat()}/{slot}",
        now=et_datetime(d, hhmm).astimezone(UTC) + timedelta(minutes=2),
    )


def snap_problems(root: Path, now: datetime) -> list[str]:
    return [p for p in check(root, now=now) if p.startswith("snapshot slot")]


def seed_prior_day(root: Path, d: date) -> None:
    """Mark the previous trading day's slots done so only the day under test can be late."""
    prev = date(2026, 11, 2) if d == date(2026, 11, 3) else date(2026, 11, 25)
    hb_ok(root, prev, "open", "09:45")
    hb_ok(root, prev, "close", "15:50")


def test_watchdog_flags_a_missed_slot_and_clears_when_run(root: Path) -> None:
    d = date(2026, 11, 3)
    seed_prior_day(root, d)
    now = utc(d, "11:00")  # 75 min after the 09:45 slot
    assert any("`open`" in p for p in snap_problems(root, now))  # nothing ran today
    hb_ok(root, d, "open", "09:45")
    assert snap_problems(root, now) == []


def test_watchdog_respects_grace_and_late_slot(root: Path) -> None:
    d = date(2026, 11, 3)
    seed_prior_day(root, d)
    hb_ok(root, d, "open", "09:45")
    assert snap_problems(root, utc(d, "16:00")) == []  # 15:50 slot not yet late (grace 45m)
    problems = snap_problems(root, utc(d, "17:00"))
    assert len(problems) == 1 and "`close`" in problems[0] and "15:50" in problems[0]


def test_watchdog_ignores_weekends_holidays_and_uses_half_day_time(root: Path) -> None:
    hb_ok(root, date(2026, 11, 25), "open", "09:45")
    hb_ok(root, date(2026, 11, 25), "close", "15:50")
    # Thanksgiving (11-26) expects nothing; the next expectations are on the 11-27 half-day
    assert snap_problems(root, utc(date(2026, 11, 26), "20:00")) == []
    d = date(2026, 11, 27)  # half-day: close slot at 12:50, not 15:50
    hb_ok(root, d, "open", "09:45")
    assert any("12:50" in p for p in snap_problems(root, utc(d, "14:00")))
    hb_ok(root, d, "close", "12:50")
    assert snap_problems(root, utc(d, "14:00")) == []
    assert (
        snap_problems(root, utc(date(2026, 11, 28), "15:00")) == []
    )  # Saturday: nothing new is due


def test_watchdog_reports_recent_failure(root: Path) -> None:
    d = date(2026, 11, 3)
    seed_prior_day(root, d)
    hb_ok(root, d, "open", "09:45")
    heartbeat.record(
        root,
        "snapshot",
        status="failed",
        slot_key=f"{d}/close",
        detail="QQQ: boom",
        now=utc(d, "15:55"),
    )
    problems = check(root, now=utc(d, "16:10"))
    assert any("FAILED" in p and "QQQ: boom" in p for p in problems)


def test_weekly_report_expectation(root: Path) -> None:
    from odj.common.config import journal_cfg

    sun = utc(date(2026, 11, 8), "21:00")  # Sunday 16:00 ET: before the 19:00 slot
    assert weekly_expected(sun, journal_cfg(root)) != [et_datetime(date(2026, 11, 8), "19:00")]
    assert et_datetime(date(2026, 11, 1), "19:00") in weekly_expected(sun, journal_cfg(root))
    late = utc(date(2026, 11, 9), "05:00")  # Monday 00:00 ET, > 3h after Sunday 19:00
    assert any("weekly report" in p for p in check(root, now=late))
    heartbeat.record(
        root,
        "weekly_report",
        status="ok",
        slot_key="2026-W45",
        now=utc(date(2026, 11, 8), "19:05").replace(tzinfo=UTC),
    )
    assert not any("weekly report" in p for p in check(root, now=late))


def test_build_alert_roundtrips_through_the_real_schema(root: Path) -> None:
    from odj.common.schema import validate

    a = client.build_alert(
        "spy-bps-bot",
        "S1",
        "spy",
        "put_credit_spread",
        [client.leg("sell", "put", 530, "2026-11-20"), client.leg("buy", "put", 525, "2026-11-20")],
        0.9,
        contracts=2,
        max_profit=90,
        max_loss=410,
        profit_target_pct=50,
        stop_multiple=2.0,
        dte_exit=7,
        confidence=70,
        gate="on",
        n=3,
        now=datetime(2026, 11, 2, 14, 35, tzinfo=UTC),
        test=True,
    )
    assert (
        a["id"] == "spy-bps-bot-20261102T143500Z-3"
        and a["underlying"] == "SPY"
        and not client.validate_light(a)
    )
    assert validate("alert", a, root)["exit_rules"] == {
        "profit_target_pct_of_credit": 50,
        "stop_loss_multiple_of_credit": 2.0,
        "dte_exit": 7,
    }
    stock = client.build_alert(
        "ibs-bot", "IBS", "AAPL", "equity_long", [client.leg("buy", "stock")], -190.5, basis="last"
    )
    assert validate("alert", stock, root)["structure"] == "equity_long"
    straddle = client.build_alert(
        "pre-earnings",
        "straddle",
        "NVDA",
        "long_straddle",
        [client.leg("buy", "call", 900, "2026-11-20"), client.leg("buy", "put", 900, "2026-11-20")],
        -42.0,
    )
    assert validate("alert", straddle, root)["alert_net_price"] == -42.0
