#!/usr/bin/env python3
"""Regression tests for force_background_sleep.py.

Run: python3 tests/test_force_background_sleep.py
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "force_background_sleep.py"
sys.path.insert(0, str(HOOK.parent))
from force_background_sleep import is_watchdog  # noqa: E402


def ok(label, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        sys.exit(1)


FIRE = [
    ("bare sleep", "sleep 60 && echo check"),
    ("poll loop", "until grep -q Ready log; do sleep 1; done"),
    ("loop after other work", "pkill -A -f server; for i in $(seq 40); do sleep 0.25; done; curl -s localhost"),
    ("loop after a heredoc", "cat > x.sh <<'EOF'\necho hi\nEOF\nwhile ! test -f done.flag; do sleep 2; done"),
]
SILENT = [
    ("sleep inside a command", "pkill -f foo; sleep 2; uv run server"),
    # the 2026-09-22 false positive: test fixtures written through a heredoc
    ("python heredoc with fixture strings", "python3 - <<'EOF'\nFIX = \"until ! pgrep -f x; do sleep 3; done\"\nEOF\npython3 tests/run.py"),
    ("script written through a heredoc", "cat > wait.sh <<'EOF'\nwhile true; do sleep 5; done\nEOF\nchmod +x wait.sh"),
    # 2026-09-25 delegation-audit false positive: a log line that mentions a loop
    ("loop text inside an echo", 'echo "step 3: for i in 1 2 3; do sleep 20 ..." >> log.txt'),
]

for label, cmd in FIRE:
    ok(f"fire: {label}", is_watchdog(cmd))
for label, cmd in SILENT:
    ok(f"silent: {label}", not is_watchdog(cmd))


def run_hook(payload):
    r = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True, text=True, check=True)
    return json.loads(r.stdout)["hookSpecificOutput"] if r.stdout.strip() else None


out = run_hook({"tool_name": "Bash", "tool_input": {"command": "sleep 30"}})
ok("end to end: backgrounds", out and out["updatedInput"]["run_in_background"] is True, repr(out))
# subagents are re-invoked by their task notifications on 2.1.280, so they get it too
# (in-process teammates are skipped: tests/test_agent_kind.py covers the classification)
out = run_hook({"tool_name": "Bash", "agent_id": "a123", "tool_input": {"command": "sleep 30"}})
ok("end to end: subagent backgrounds too", out and out["updatedInput"]["run_in_background"] is True, repr(out))
ok("end to end: teammate backgrounded", run_hook({"tool_name": "Bash", "agent_id": "w@team", "tool_input": {"command": "sleep 30"}}) is not None)
ok("end to end: heredoc silent", run_hook({"tool_name": "Bash", "tool_input": {"command": SILENT[1][1]}}) is None)

# 2.1.314+: a Monitor's command reaches Bash hooks as tool_name "Bash"; the transcript's
# tool_use block (by tool_use_id) is the only thing that says Monitor.
with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
    for issuer, tid in [("Monitor", "toolu_mon"), ("Bash", "toolu_bash")]:
        f.write(json.dumps({"type": "assistant", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "id": tid, "name": issuer, "input": {"command": "until x; do sleep 5; done"}}]}}) + "\n")
mon = {"tool_name": "Bash", "tool_use_id": "toolu_mon", "transcript_path": f.name,
       "tool_input": {"command": "until x; do sleep 5; done", "description": "watch"}}
ok("end to end: Monitor loop left alone", run_hook(mon) is None)
ok("end to end: same loop from Bash still backgrounded", run_hook({**mon, "tool_use_id": "toolu_bash"}) is not None)
print("all passed")
