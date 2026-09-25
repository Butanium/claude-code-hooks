#!/usr/bin/env python3
"""force_background_task.py: silent on every shape, never blocks.

Run: python3 tests/test_force_background_task.py
"""
import json
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "force_background_task.py"


def run(payload):
    res = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload) if not isinstance(payload, str) else payload,
                         capture_output=True, text=True)
    return res.returncode, (json.loads(res.stdout) if res.stdout.strip() else None)


def check(label, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        sys.exit(1)


# 2.1.280 shape: no run_in_background key at all
rc, out = run({"tool_name": "Agent", "tool_input": {"description": "x", "prompt": "y", "subagent_type": "general-purpose"}})
hso = (out or {}).get("hookSpecificOutput", {})
check("absent key: exit 0", rc == 0)
check("absent key: flag set via updatedInput", hso.get("updatedInput", {}).get("run_in_background") is True, f"out={out}")
check("absent key: no additionalContext", "additionalContext" not in hso, f"out={out}")

rc, out = run({"tool_name": "Agent", "tool_input": {"description": "x", "prompt": "y", "run_in_background": True}})
check("already background: no output", rc == 0 and out is None, f"rc={rc} out={out}")

rc, out = run({"tool_name": "Agent", "tool_input": {"description": "x", "prompt": "y", "name": "t1", "isolation": "worktree"}})
check("worktree teammate candidate: left to agent_worktree_teammate", rc == 0 and out is None, f"rc={rc} out={out}")

rc, out = run({"tool_name": "Bash", "tool_input": {"command": "ls"}})
check("other tool: no output", rc == 0 and out is None)

rc, out = run("not json")
check("malformed input: non-blocking error (exit 1, no deny)", rc == 1 and out is None, f"rc={rc}")

print("ALL OK")
