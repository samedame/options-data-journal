from __future__ import annotations

import shutil
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A scratch copy of config/ + schemas/ so tests never touch the real data directories."""
    for d in ("config", "schemas"):
        shutil.copytree(REPO / d, tmp_path / d)
    jcfg = tmp_path / "config" / "journal.yml"  # tests use dates before the real go-live instant
    jcfg.write_text(
        jcfg.read_text().replace(
            'not_before_utc: "2026-11-04T12:00:00Z"', 'not_before_utc: "2000-01-01T00:00:00Z"'
        )
    )
    monkeypatch.setenv("ODJ_ROOT", str(tmp_path))
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    return tmp_path
