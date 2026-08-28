#!/usr/bin/env python3
"""Regression tests for no_tail_head_pipes.py.

The fixtures are anonymized shapes from a 100-firing audit of the old
`\\|\\s*(tail|head)\\b` regex (ENGINEERING_LOGS.md, 2026-08-28): ~35% of its
denials were pipes that never touched the long command's output — a `grep`
over a log the command had already redirected, `git log | head` in front of
the real work, a `tmux capture-pane | tail` poll, a `| tail -1` inside a
quoted `$(...)`. Each FIRE fixture is a shape where the pipe really would
have truncated a long command's output; each SILENT fixture is a shape the
old regex denied for nothing.

Run: python3 tests/test_no_tail_head_pipes.py
"""

import json
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "no_tail_head_pipes.py"
sys.path.insert(0, str(HOOK.parent))
from no_tail_head_pipes import should_fire  # noqa: E402


def check(label, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        sys.exit(1)


FIRE = [
    ("plain long command", "uv run pytest -q 2>&1 | tail -3"),
    ("script via interpreter path", ".venv/bin/python scripts/check.py 2>&1 | tail -20"),
    ("timeout wrapper", "timeout 150 uv run --with playwright python shot.py out/ 2>&1 | head -12"),
    ("env assignment prefix", "UV_LINK_MODE=symlink uv run python shot.py 2>&1 | tail -20"),
    ("cd then long command", "cd web && npm run -s test 2>&1 | tail -30; echo '=== CHECK ==='; npm run -s check 2>&1 | tail -15"),
    ("grep filter before tail is still truncation", "python3 tools/index.py index 2>&1 | grep -vE '^[0-9]+/' | tail -20"),
    ("positive grep then head", "npm run check 2>&1 | grep -E 'Error|error|warnings' | head -20"),
    ("long substep chained into more work", "npm run build 2>&1 | tail -1 && cd .. && timeout 140 node tv.mjs http://127.0.0.1:8890"),
    ("install output", "uv pip install --python .venv/bin/python -e '.[pdf]' 2>&1 | tail -20"),
    ("shell script producer", "scripts/smoke.sh browser_a browser_b 2>&1 | tail -40"),
    ("for loop body", "for i in 1 2 3; do uv run python tests/smoke.py http://127.0.0.1:8917 2>&1 | tail -5; done"),
    ("sudo wrapper", "sudo apt-get install -y texlive-fonts-extra 2>&1 | tail -5"),
    ("only first statement is a reader", "git log --oneline -3; uv run pytest -q 2>&1 | tail -3"),
    ("heredoc script then run piped", "cat > dbg.py <<'EOF'\nimport sys\nprint('x')\nEOF\npython dbg.py 2>&1 | grep -vE 'warn' | tail -30"),
]

SILENT = [
    ("NEEDTAIL escape hatch", "uv run pytest -q 2>&1 | tail -3  # NEEDTAIL"),
    ("no pipe at all", "uv run pytest -q > out.log 2>&1; tail -3 out.log"),
    ("grep over an already-redirected log", "timeout 600 pytest tests/ -q > pytest1.log 2>&1; echo \"exit=$?\"; grep -n 'passed\\|failed\\|error' pytest1.log | tail -5"),
    ("cat of a task output file", "sleep 15; cat /tmp/tasks/bho0bwml2.output 2>/dev/null | tail -15"),
    ("cat of a saved log", "cd web && npm test > unit.log 2>&1; echo \"test=$?\"; cat unit.log | tail -30"),
    ("grep of a saved log then head", "bibtex main > bib.log 2>&1; echo \"bibtex exit=$?\"; grep -iE 'error|warning' bib.log | head -20; ls -la main.bbl"),
    ("git plumbing before the real work", "git merge --ff-only fix 2>&1 | tail -1 && git log --oneline -1 && (uv run pytest -q > f7.log 2>&1; echo \"pytest $?: $(tail -1 f7.log)\")"),
    ("git status head", "git stash list | head -2; git status --short | head -20; scripts/smoke.sh --baseline HEAD > baseline1.log 2>&1"),
    ("pgrep before the real work", "pgrep -af 'server --port 8791' | head; echo '--- rerun'; scripts/smoke.sh a b c > /var/tmp/final.log 2>&1"),
    ("tmux capture-pane poll loop", "until tmux capture-pane -t s -p | tail -5 | grep -qE 'startup complete|Traceback'; do sleep 3; done; tmux capture-pane -t s -p | tail -20"),
    ("while condition with pipe", "while squeue --me -h -j 44576 2>/dev/null | grep -q .; do sleep 5; done && echo done && grep -n 'error' run.log | head -10"),
    ("pipe inside command substitution", "for i in $(seq 1 30); do sha=$(curl -s https://example.org/ | grep -o 'app\\.[A-Za-z0-9_]*\\.js' | head -1); echo \"$i $sha\"; sleep 15; done"),
    ("pipe inside quoted substitution, real command unpiped", "export PATH=\"$(ls -d \"$HOME\"/.nvm/versions/node/v*/bin | sort -V | tail -1):$PATH\" && npm run build"),
    ("pipe inside a heredoc body", "cat > run.sh <<'EOF'\n#!/bin/bash\ngrep -c done out.txt | tail -1\nEOF\nbash run.sh > run.log 2>&1"),
    ("curl health probe", "sleep 8; curl -s http://127.0.0.1:8901/api/health | head -c 60"),
    ("head of a downloaded file", "curl -s -o live.pdf http://127.0.0.1:8766/report.pdf; head -c 200 live.pdf | cat -v | head -3"),
    ("ls tail", "ls /tmp/smoke-*/browser.log | tail -1"),
    ("sacct head", "sacct -j 44072 --format=State -P -n | head -1 > /tmp/state.log; crun uv run experiments/quantify.py --exp fish"),
    ("pipe char inside grep pattern only", "uv run pytest -q > out.log 2>&1; grep -E 'passed|failed' out.log"),
    ("non-Bash-ish reader tool", "whowas show abc:790 -C 4 2>&1 | cut -c1-900 | head -70"),
]


for label, cmd in FIRE:
    check(f"fires: {label}", should_fire(cmd), f"cmd={cmd!r}")

for label, cmd in SILENT:
    check(f"silent: {label}", not should_fire(cmd), f"cmd={cmd!r}")


# --- end-to-end through stdin/stdout -------------------------------------------
def run_hook(payload):
    res = subprocess.run(
        [sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True, text=True
    )
    assert res.returncode == 0, f"hook exited {res.returncode}: {res.stderr}"
    return json.loads(res.stdout) if res.stdout.strip() else None


def bash(command, background=True):
    inp = {"command": command}
    if background:
        inp["run_in_background"] = True
    return run_hook({"tool_name": "Bash", "tool_input": inp})


out = bash("uv run pytest -q 2>&1 | tail -3")
hso = (out or {}).get("hookSpecificOutput", {})
check("e2e: denies a background truncating pipe", hso.get("permissionDecision") == "deny", f"out={out}")
check("e2e: names the event", hso.get("hookEventName") == "PreToolUse")
check("e2e: reason mentions NEEDTAIL", "NEEDTAIL" in hso.get("permissionDecisionReason", ""))
check("e2e: foreground is not the hook's business", bash("uv run pytest -q 2>&1 | tail -3", background=False) is None)
check("e2e: reader pipe passes", bash("cat out.log | tail -3") is None)
check("e2e: NEEDTAIL passes", bash("uv run pytest -q 2>&1 | tail -3  # NEEDTAIL") is None)
check("e2e: other tools ignored", run_hook({"tool_name": "Read", "tool_input": {"file_path": "x | tail"}}) is None)

print("ALL OK")
