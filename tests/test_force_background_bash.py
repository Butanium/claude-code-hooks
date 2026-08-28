#!/usr/bin/env python3
"""Regression tests for force_background_bash.py's kill-class detection.

Each fixture is a command probed live against the CLI (2.1.250, 2026-08-28):
5s sync timeout, 12s job, and the observed outcome — `kill` = "Command timed
out after 5s" / exit 143, `background` = "moved to the background". The hook
must deny (not clamp) the kill class and leave the background class alone.
A false `kill` here is safe (deny-with-advice); a false `background` turns a
long timeout into a 30s SIGTERM, so when in doubt the fixture goes in KILL.

Run: python3 tests/test_force_background_bash.py
"""

import json
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "force_background_bash.py"
sys.path.insert(0, str(HOOK.parent))
from force_background_bash import is_kill_class  # noqa: E402


def check(label, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        sys.exit(1)


SLEEP = 'python3 -c "import time; time.sleep(12)"'
HD = "import time\ntime.sleep(12)\nprint('done')\nEOF"

KILL = [
    ("$VAR redirect target", f"S=/tmp/x; {SLEEP} > $S/out 2>&1; echo done"),
    ("git in the chain", f"git -C /tmp status --short > /dev/null; {SLEEP}; echo done"),
    ("unquoted heredoc, redirect", f"python3 - > /tmp/x/out 2>&1 <<EOF\n{HD}"),
    ("unquoted heredoc, no redirect", f"python3 - <<EOF\n{HD}"),
    ("quoted heredoc, redirect AFTER the operator", f"python3 - <<'EOF' > /tmp/x/out\n{HD}"),
    ("quoted heredoc, $VAR redirect before", f"S=/tmp/x; python3 - > $S/out <<'EOF'\n{HD}"),
    ("leading sleep", "sleep 30 && echo check"),
    ("process substitution", "diff <(sort a) <(sort b)"),
    ("herestring (untested, kept conservative)", "python3 - <<< 'print(1)'"),
]

BACKGROUND = [
    ("literal redirect path", f"{SLEEP} > /tmp/x/out 2>&1; echo done"),
    ("quoted heredoc, stdout redirect before", f"python3 - > /tmp/x/out <<'EOF'\n{HD}"),
    ("quoted heredoc, 2>&1 before", f"python3 - > /tmp/x/out 2>&1 <<'EOF'\n{HD}"),
    ("quoted heredoc in a chain", f"cd /tmp/x && echo start; python3 - > out <<'EOF'\n{HD}\necho \"exit=$?\""),
    ("quoted heredoc, no redirect", f"python3 - <<'EOF'\n{HD}"),
    ("quoted heredoc, shell-looking body", "python3 - > /tmp/x/out 2>&1 <<'EOF'\nx = [i for i in range(9) if i > 3]\ns = \"$HOME `whoami` a | b > c\"\nprint(x, s)\nEOF"),
    ("quoted heredoc then a later literal redirect", f"python3 - <<'EOF'\nprint('patch')\nEOF\n{SLEEP} > /tmp/x/out 2>&1; echo done"),
]

for label, cmd in KILL:
    check(f"kill class: {label}", is_kill_class(cmd), f"cmd={cmd!r}")
for label, cmd in BACKGROUND:
    check(f"backgroundable: {label}", not is_kill_class(cmd), f"cmd={cmd!r}")


# --- end-to-end -----------------------------------------------------------------
def run_hook(payload):
    res = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True, text=True)
    assert res.returncode == 0, f"hook exited {res.returncode}: {res.stderr}"
    return json.loads(res.stdout) if res.stdout.strip() else None


def bash(command, timeout):
    return run_hook({"tool_name": "Bash", "tool_input": {"command": command, "timeout": timeout}})


out = bash(KILL[0][1], 120000)
hso = (out or {}).get("hookSpecificOutput", {})
check("e2e: kill class with long timeout is denied", hso.get("permissionDecision") == "deny", f"out={out}")
check("e2e: advice names the safe heredoc form", "<<'EOF'" in hso.get("permissionDecisionReason", ""))

out = bash(BACKGROUND[2][1], 120000)
hso = (out or {}).get("hookSpecificOutput", {})
check("e2e: safe heredoc with long timeout is clamped, not denied", hso.get("updatedInput", {}).get("timeout") == 30000, f"out={out}")

check("e2e: short timeout is left alone", bash(KILL[0][1], 5000) is None)

print("ALL OK")
