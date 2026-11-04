from __future__ import annotations

import pytest

from odj.selftest import run_selftest


def test_selftest_passes_offline(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import socket

    def no_network(*a: object, **k: object) -> None:
        raise AssertionError("selftest must not touch the network")

    monkeypatch.setattr(socket, "create_connection", no_network)
    assert run_selftest() == 0
    out = capsys.readouterr().out
    assert "FAIL" not in out and out.strip().endswith("PASS")
