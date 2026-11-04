"""Discord webhook helper: rate limited, length truncated, distinct tone for info vs failure.

The webhook URL comes only from $DISCORD_WEBHOOK_URL and is never logged.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from typing import Any, Literal

import requests

log = logging.getLogger("odj.discord")
Level = Literal["info", "warn", "fail"]

MAX_LEN = 1900  # Discord hard limit is 2000 chars; leave headroom for the prefix
MIN_INTERVAL = 1.2  # seconds between posts from this process
_PREFIX: dict[str, str] = {
    "info": "ℹ️ ",
    "warn": "⚠️ **WARNING** — ",
    "fail": "🚨 **FAILURE** — ",
}
_lock = threading.Lock()
_last_post = 0.0


def truncate(text: str, limit: int = MAX_LEN) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 20].rstrip() + "\n…[truncated]"


def format_message(text: str, level: Level = "info") -> str:
    return truncate(_PREFIX[level] + text)


def post(text: str, level: Level = "info", *, session: Any = None) -> bool:
    """Post to Discord. Returns False (never raises) if unconfigured or the request fails."""
    global _last_post
    url = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()
    if not url:
        log.warning("DISCORD_WEBHOOK_URL not set; message not sent (%s): %s", level, text[:120])
        return False
    payload = {"content": format_message(text, level), "allowed_mentions": {"parse": []}}
    http = session or requests
    with _lock:
        wait = MIN_INTERVAL - (time.monotonic() - _last_post)
        if wait > 0:
            time.sleep(wait)
        for _attempt in range(3):
            try:
                resp = http.post(url, json=payload, timeout=10)
            except requests.RequestException as exc:
                # never include the URL: exception text can embed it
                log.error("discord post failed: %s", type(exc).__name__)
                return False
            _last_post = time.monotonic()
            if resp.status_code == 429:
                try:
                    retry = float(resp.json().get("retry_after", 1.5))
                except Exception:
                    retry = 1.5
                time.sleep(min(retry, 10))
                continue
            if resp.status_code >= 300:
                log.error("discord post returned HTTP %s", resp.status_code)
                return False
            return True
    return False
