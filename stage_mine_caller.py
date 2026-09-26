#!/usr/bin/env python3
"""PreToolUse(Bash): record which agent loop is about to run `stage-mine`.

A subagent's Bash commands run in its parent's process, with the parent's
$CLAUDE_CODE_SESSION_ID and environment, and the running call isn't in any
transcript yet, so stage-mine can't tell on its own that a subagent called it;
it then attributed the parent's edits to the subagent's commit (2026-09-25).
Hook input does carry the caller's agent_id. This appends
{agent_id, transcript, t, command} to <state dir>/stage-mine-callers/<session>.jsonl,
and stage-mine reads the newest entry. Prints nothing; fails open.
"""
import json
import os
import sys
import time
from pathlib import Path

STATE = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "state" / "stage-mine-callers"


def main() -> None:
    data = json.load(sys.stdin)
    cmd = (data.get("tool_input") or {}).get("command") or ""
    sid = data.get("session_id")
    if data.get("tool_name") != "Bash" or "stage-mine" not in cmd or not sid:
        return
    STATE.mkdir(parents=True, exist_ok=True)
    path = STATE / f"{sid}.jsonl"
    entry = {"agent_id": data.get("agent_id") or None, "transcript": data.get("transcript_path"),
             "t": time.time(), "command": cmd[:500]}
    lines = path.read_text().splitlines()[-19:] if path.exists() else []
    tmp = path.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text("".join(line + "\n" for line in [*lines, json.dumps(entry)]))
    os.replace(tmp, path)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
