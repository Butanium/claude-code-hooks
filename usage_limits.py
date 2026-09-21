#!/usr/bin/env python3
"""Model-scoped weekly usage windows for the status line.

The statusline payload only carries the account-wide windows (`five_hour`,
`seven_day`); the per-model weekly window that `/usage` shows as
"Current week (Fable)" is not in it. It lives in `GET /api/oauth/usage`, so we
fetch it ourselves, cache it, and let the status line render from the cache.

Run as a script, this performs the fetch (that is what the status line spawns
in the background).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

CACHE_DIR = Path.home() / ".cache" / "claude-status"
CACHE = CACHE_DIR / "usage-limits.json"
LOCK = CACHE_DIR / "usage-limits.lock"
CREDS = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / ".credentials.json"

ENDPOINT = "https://api.anthropic.com/api/oauth/usage"
REFRESH_AFTER = 60  # cache older than this triggers a background refresh
STALE_AFTER = 900  # still rendered past this, but marked with '?'
LOCK_TIMEOUT = 60  # a refresher holding the lock this long is assumed dead


def _token() -> str | None:
    try:
        oauth = json.loads(CREDS.read_text()).get("claudeAiOauth") or {}
    except Exception:
        return None
    expires_at = oauth.get("expiresAt")
    if expires_at and expires_at / 1000 <= time.time():
        return None  # the CLI refreshes it; we never touch the credential file
    return oauth.get("accessToken")


def fetch() -> dict:
    """{'fetched_at': …, 'scoped': [{'model': 'Fable', 'percent': 15, …}]}"""
    token = _token()
    if not token:
        return {"fetched_at": time.time(), "scoped": []}

    req = urllib.request.Request(
        ENDPOINT,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        data = json.load(resp)

    scoped = []
    for limit in data.get("limits") or []:
        if limit.get("kind") != "weekly_scoped" or limit.get("percent") is None:
            continue
        model = ((limit.get("scope") or {}).get("model") or {}).get("display_name")
        if model:
            scoped.append(
                {"model": model, "percent": limit["percent"], "resets_at": limit.get("resets_at")}
            )
    return {"fetched_at": time.time(), "scoped": scoped}


def _write(payload: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CACHE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload))
    tmp.replace(CACHE)


def _spawn_refresh() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if time.time() - LOCK.stat().st_mtime < LOCK_TIMEOUT:
            return
    except FileNotFoundError:
        pass
    try:
        LOCK.write_text(str(os.getpid()))
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve())],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        LOCK.unlink(missing_ok=True)


def scoped_parts() -> list[str]:
    """['7dFable:15%'] — one token per model-scoped weekly window."""
    try:
        cache = json.loads(CACHE.read_text())
        age = time.time() - cache["fetched_at"]
    except Exception:
        cache, age = None, None

    if age is None or age > REFRESH_AFTER:
        _spawn_refresh()
    if not cache:
        return []

    mark = "?" if age > STALE_AFTER else ""
    return [
        f"7d{e['model'].replace(' ', '')}:{e['percent']:.0f}%{mark}"
        for e in cache.get("scoped", [])
    ]


if __name__ == "__main__":
    try:
        _write(fetch())
    finally:
        LOCK.unlink(missing_ok=True)
