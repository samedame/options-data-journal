"""CI helper: commit and push generated data with bounded fetch/rebase/push retries.

Why not just `git pull --rebase && git push`?  Several workflows (snapshot, alert ingest, issue
ingest) can finish at the same minute. GitHub's `concurrency` keeps only ONE pending run per group
(older pending runs are cancelled even with cancel-in-progress: false), so serialising alert
ingests with it could silently DROP alerts. Instead each run commits independently and this
helper resolves the races:

* journal/**/*.jsonl are append-only and use `merge=union` (.gitattributes): concurrent appends
  from two runs are both kept.
* conflicting hunks elsewhere (heartbeat, summary) are resolved in favour of our commit
  (`-X theirs` in a rebase = the commit being replayed), then derived files are regenerated.
"""

from __future__ import annotations

import json
import logging
import os
import random
import subprocess
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from odj.common import heartbeat as hb
from odj.common.io import write_json

log = logging.getLogger("odj.ci")
DEFAULT_PATHS = ("journal", "data", "status", "reports")


class PublishError(RuntimeError):
    pass


def _git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env.setdefault("GIT_TERMINAL_PROMPT", "0")
    name = env.get("ODJ_GIT_NAME")
    email = env.get("ODJ_GIT_EMAIL")
    prefix = []
    if name:
        prefix += ["-c", f"user.name={name}"]
    if email:
        prefix += ["-c", f"user.email={email}"]
    cp = subprocess.run(
        ["git", *prefix, *args], cwd=root, capture_output=True, text=True, env=env, check=False
    )
    if check and cp.returncode != 0:
        raise PublishError(f"git {' '.join(args)} failed: {cp.stderr.strip()[:400]}")
    return cp


def publish(
    root: Path,
    message: str,
    *,
    paths: Sequence[str] = DEFAULT_PATHS,
    regen: Callable[[], None] | None = None,
    attempts: int = 6,
    remote: str = "origin",
    sleep: Callable[[float], None] = time.sleep,
    push: bool = True,
) -> dict[str, object]:
    """Stage `paths`, commit, then fetch/rebase/push with bounded retries. Returns a status dict."""
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    existing = [p for p in paths if (root / p).exists()]
    committed = False
    for attempt in range(1, attempts + 1):
        _git(root, "add", "-A", "--", *existing)
        staged = _git(root, "diff", "--cached", "--quiet", check=False).returncode != 0
        if staged:
            _git(root, "commit", "-m", message)
            committed = True
        if not push:
            return {"status": "committed" if committed else "nothing-to-commit", "attempt": attempt}
        ahead = _git(root, "rev-list", "--count", f"{remote}/{branch}..HEAD", check=False)
        if not committed and ahead.returncode == 0 and ahead.stdout.strip() == "0":
            return {"status": "nothing-to-commit", "attempt": attempt}
        fetched = _git(root, "fetch", remote, branch, check=False)
        if fetched.returncode == 0:
            mine_sha = _git(root, "rev-parse", "HEAD").stdout.strip()
            rebase = _git(root, "rebase", "-X", "theirs", f"{remote}/{branch}", check=False)
            if rebase.returncode != 0:
                _git(root, "rebase", "--abort", check=False)
                log.warning("rebase failed on attempt %d: %s", attempt, rebase.stderr.strip()[:200])
                sleep(random.uniform(1, 3))
                continue
            _remerge_heartbeat(root, remote, branch, mine_sha)
            if regen is not None:
                regen()
                _git(root, "add", "-A", "--", *existing)
                if _git(root, "diff", "--cached", "--quiet", check=False).returncode != 0:
                    _git(root, "commit", "-m", f"{message} (regenerate derived files)")
        pushed = _git(root, "push", remote, f"HEAD:{branch}", check=False)
        if pushed.returncode == 0:
            return {"status": "pushed", "attempt": attempt}
        log.warning("push rejected on attempt %d: %s", attempt, pushed.stderr.strip()[:200])
        sleep(random.uniform(0.5, 2.5) * attempt)
    raise PublishError(f"could not push after {attempts} attempts")


def _show_json(root: Path, ref: str, rel: str) -> dict[str, Any] | None:
    cp = _git(root, "show", f"{ref}:{rel}", check=False)
    if cp.returncode != 0:
        return None
    try:
        data = json.loads(cp.stdout)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _remerge_heartbeat(root: Path, remote: str, branch: str, mine_sha: str) -> None:
    """`-X theirs` keeps OUR side of a conflicting hunk, which would drop the other run's heartbeat
    entries. Re-derive status/heartbeat.json as a semantic three-way merge instead."""
    rel = "status/heartbeat.json"
    up = _show_json(root, f"{remote}/{branch}", rel)
    mine = _show_json(root, mine_sha, rel)
    if up is None or mine is None:
        return
    base_sha = _git(root, "merge-base", mine_sha, f"{remote}/{branch}", check=False).stdout.strip()
    base = _show_json(root, base_sha, rel) if base_sha else None
    merged = hb.merge(base, up, mine)
    current = _show_json(root, "HEAD", rel)
    if merged != current:
        write_json(root / rel, merged)
        _git(root, "add", "--", rel)
        _git(root, "commit", "-m", "merge heartbeat state")
