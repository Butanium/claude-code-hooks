#!/usr/bin/env python3
"""Deliver team messages mid-turn, through a side channel next to the CLI's inbox.

The CLI's inbox poller moves a busy member's messages into memory and prunes
them from inboxes/<name>.json within a second, then hands them over only when
the member ends its turn; a "stop, the plan changed" lands after the work is
done. Two PostToolUse entry points, one file:

- PostToolUse(SendMessage), sender side: a message that was delivered to a
  team member is also appended to teams/<team>/midturn/<recipient>.jsonl.
- PostToolUse(any tool), recipient side: entries the session hasn't been shown
  (and hasn't already received the normal way) are injected as
  additionalContext and recorded in midturn/<name>.injected.json.

The CLI still delivers the same message when the turn ends, and that can't be
suppressed from a hook: idle deliveries of team messages don't pass through
UserPromptSubmit (probed on 2.1.280 with a pane teammate: project PostToolUse
hooks ran, UserPromptSubmit never did). The injected text says so, so the
repeat reads as a repeat.

Every path fails open: an exception exits 0 with no output, so a broken side
channel never blocks a SendMessage or a tool result.
"""
import hashlib
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from utils._team_identity import TEAMS, identity, members  # noqa: E402

BLOCK_RE = re.compile(r'<teammate-message\s+teammate_id="([^"]+)"[^>]*>\n?(.*?)\n?</teammate-message>', re.S)
MAX_AGE_S = 6 * 3600  # never inject side-channel entries older than this


def norm(text: str) -> str:
    return " ".join(text.split())


def entry_id(sender: str, text: str) -> str:
    return hashlib.sha1(f"{sender}\0{norm(text)}".encode()).hexdigest()[:16]


def channel(team: str, name: str) -> Path:
    return TEAMS / team / "midturn" / f"{name}.jsonl"


def state_path(team: str, name: str) -> Path:
    return TEAMS / team / "midturn" / f"{name}.injected.json"


def load_state(team: str, name: str) -> dict:
    try:
        return json.loads(state_path(team, name).read_text())
    except (OSError, ValueError):
        return {}


def save_state(team: str, name: str, state: dict) -> None:
    p = state_path(team, name)
    tmp = p.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(state))
    os.replace(tmp, p)


# --- sender -----------------------------------------------------------------------

def record_sent(data: dict) -> None:
    me = identity(data)
    if not me:
        return
    team, sender = me
    inp = data.get("tool_input") or {}
    resp = data.get("tool_response") or {}
    if isinstance(resp, str):
        try:
            resp = json.loads(resp)
        except ValueError:
            return
    text = inp.get("message")
    if not isinstance(text, str) or not resp.get("success"):
        return  # protocol frames (dicts) and failed sends stay out
    target = ((resp.get("routing") or {}).get("target") or "").lstrip("@") or inp.get("to", "")
    if target in ("", "*", sender) or target not in members(team):
        return
    path = channel(team, target)
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {"id": entry_id(sender, text), "from": sender, "summary": inp.get("summary") or "",
             "text": text, "t": time.time()}
    with path.open("a") as f:  # one short O_APPEND write per message
        f.write(json.dumps(entry) + "\n")


# --- recipient ----------------------------------------------------------------------

def already_received(transcript: str | None, entries: list[dict]) -> set[str]:
    """Ids among `entries` that already reached this session the normal way."""
    if not transcript or not entries:
        return set()
    try:
        with open(transcript, "rb") as f:
            f.seek(0, 2)
            f.seek(max(0, f.tell() - 4_000_000))
            tail = f.read().decode("utf-8", "replace")
    except OSError:
        return set()
    seen = set()
    for line in tail.splitlines():
        if "<teammate-message" not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if rec.get("type") != "user":
            continue
        content = (rec.get("message") or {}).get("content")
        text = content if isinstance(content, str) else " ".join(
            b.get("text", "") for b in content or [] if isinstance(b, dict))
        for sender, body in BLOCK_RE.findall(text):
            seen.add(entry_id(sender, body))
    return {e["id"] for e in entries} & seen


def inject(data: dict) -> None:
    # Runs after every tool call of every session: only look for a lead among teams
    # that have a message waiting for their lead.
    me = identity(data, lead_configs=[p.parent.parent / "config.json"
                                      for p in TEAMS.glob("*/midturn/team-lead.jsonl")])
    if not me:
        return
    team, name = me
    path = channel(team, name)
    if not path.exists():
        return
    sid = data.get("session_id") or ""
    state = load_state(team, name)
    done = set(state.get(sid, []))
    now = time.time()
    fresh = []
    for line in path.read_text().splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if e.get("id") not in done and now - e.get("t", 0) < MAX_AGE_S:
            fresh.append(e)
    if not fresh:
        return
    delivered = already_received(data.get("transcript_path"), fresh)
    new = [e for e in fresh if e["id"] not in delivered]
    state[sid] = sorted(done | {e["id"] for e in fresh})[-500:]
    save_state(team, name, state)
    if not new:
        return
    blocks = "\n".join(
        f'<teammate-message teammate_id="{e["from"]}" summary="{e["summary"]}">\n{e["text"]}\n</teammate-message>'
        for e in new)
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PostToolUse",
        "additionalContext": (
            f"{'A team message' if len(new) == 1 else f'{len(new)} team messages'} arrived while you were "
            "working, delivered now instead of when your turn ends. If the harness shows it again later, "
            "that is the same message; don't act on it twice.\n" + blocks),
    }}))


def main() -> None:
    data = json.load(sys.stdin)
    if data.get("hook_event_name") != "PostToolUse":
        return
    if data.get("tool_name") == "SendMessage":
        record_sent(data)
    inject(data)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
