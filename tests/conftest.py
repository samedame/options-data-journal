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
    monkeypatch.setenv("ODJ_ROOT", str(tmp_path))
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    return tmp_path
