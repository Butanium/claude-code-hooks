#!/usr/bin/env python3
"""Replay every historical security_guard firing through the current guard.

Box-local: needs the whowas index (~/.cache/whowas/index.db) and the transcripts it
points at. Each firing is a tool_result row with denial='permission-rule' whose text
carries the guard's reason; the full command comes from the matching tool_use in the
JSONL (the index truncates tool inputs). Duplicates from forked/compacted transcripts
are collapsed on command text. Runs the real fetch and the real judge, with ntfy
topics blanked so nobody gets paged.

Run: python3 tests/smoke_security_guard_census.py
"""
import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

DB = Path(os.environ.get("WHOWAS_DB", Path.home() / ".cache" / "whowas" / "index.db"))
PROJECTS = Path.home() / ".claude" / "projects"
GUARD = Path(__file__).resolve().parent.parent / "security_guard.py"

if not DB.exists():
    sys.exit(f"no whowas index at {DB}; this smoke is box-local")

con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
rows = con.execute(
    "SELECT m.ts, m.session, m.path, m.line, f.text FROM fts f JOIN msgs m ON m.id = f.rowid "
    "WHERE m.denial = 'permission-rule' AND f.text LIKE '%Dangerous command detected%' ORDER BY m.ts"
).fetchall()


def full_command(path, line):
    lines = (PROJECTS / path).read_text(encoding="utf-8").splitlines()
    rec = json.loads(lines[line - 1])
    ids = {b.get("tool_use_id") for b in rec["message"]["content"] if isinstance(b, dict) and b.get("type") == "tool_result"}
    for i in range(line - 2, -1, -1):
        try:
            prev = json.loads(lines[i])
        except ValueError:
            continue
        if prev.get("type") != "assistant":
            continue
        for b in prev["message"].get("content", []):
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id") in ids:
                return b["input"].get("command")
    return None


cases = {}
for ts, session, path, line, text in rows:
    cmd = full_command(path, line)
    if cmd is None or cmd in cases:
        continue
    old = text.split("Dangerous command detected (", 1)[1].split(")", 1)[0] if "Dangerous command detected (" in text else "?"
    cases[cmd] = (ts[:10], old)

print(f"{len(rows)} firing rows, {len(cases)} distinct commands\n")
env = {**os.environ, "CLAUDE_HOTLINE_NTFY_TOPIC": "", "CLAUDE_HOTLINE_TOPIC": "", "CLAUDE_NTFY_TOPIC": ""}
for cmd, (date, old) in cases.items():
    payload = {"tool_name": "Bash", "tool_input": {"command": cmd}, "permission_mode": "default", "cwd": "/tmp", "session_id": "census"}
    t0 = time.time()
    res = subprocess.run([sys.executable, str(GUARD)], input=json.dumps(payload), capture_output=True, text=True, encoding="utf-8", env=env)
    out = json.loads(res.stdout) if res.stdout.strip() else None
    hso = (out or {}).get("hookSpecificOutput", {})
    if out is None:
        verdict = "ALLOWED (no pattern)"
    elif "permissionDecision" in hso:
        verdict = f"{hso['permissionDecision'].upper()}: {hso['permissionDecisionReason'][:300]}"
    else:
        verdict = f"ALLOWED by judge: {hso.get('additionalContext', '')[:300]}"
    one_line = " ".join(cmd.split())[:110]
    print(f"=== {date}  was: {old}  ({time.time() - t0:.1f}s)\n    cmd: {one_line}\n    now: {verdict}")
    if res.stderr.strip():
        print(f"    stderr: {res.stderr.strip()[:200]}")
    print()
