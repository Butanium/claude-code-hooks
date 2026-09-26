#!/usr/bin/env python3
"""partial_chain_warn.py: which failed commands get the "steps may not have run" note.

The fixtures mirror the 2026-09-25 archive replay (23 firings in 546 failed Bash
calls over six weeks, nearly all a skipped/failed commit, push, sed -i, mv or cp).

Run: python3 tests/test_partial_chain_warn.py
"""
import json
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "partial_chain_warn.py"
sys.path.insert(0, str(HOOK.parent))
from partial_chain_warn import write_steps_after_and  # noqa: E402


def check(label, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        sys.exit(1)


FIRE = [
    ("pathspec fails before commit", "git add hooks x.py && git commit -q -m 'a; b' && git push", 2),
    ("grep -c gate before sed -i", "grep -c foo f && sed -i 's/a/b/' f", 1),
    ("cd then content write, last step", "cd repo && cat > out.md <<'EOF'\nhi && bye\nEOF", 1),
    ("mv after a failing listing", "ls data/*.eval && mv data/x.eval archive/", 1),
    ("gh comment after a check", "test -f body.md && gh issue comment 5 --body-file body.md", 1),
]
SILENT = [
    ("no &&", "make; cp a b"),
    ("write before the && only", "cp a b && pytest -q"),
    ("script written then run (failure is the run)", "cd repo && cat > s.py <<'EOF'\nx = 1\nEOF\npython3 s.py"),
    ("script written then run, one line", "cd r && cat > s.py <<'EOF' && python3 s.py\nx\nEOF"),
    ("python heredoc analysis", "cd repo && python3 - <<'EOF'\nopen('f','w').write('a && git push')\nEOF"),
    ("output capture is not a write", "cd repo && uv run job.py > run.log 2>&1"),
    ("null redirects", "ls && cat f > /dev/null 2>&1"),
    ("|| branch", "false || git push"),
    ("quoted &&", "echo 'a && git push'"),
    # 2026-09-25 live false positive: the failure was the last `;` step, not the && group
    ("write in an earlier ; list", "cd hooks && sed -i 's/a/b/' f.sh; python3 t.py; env | grep -c ^X"),
    ("commit then a failing check", "git add f && git commit -m x\ngit log --oneline -1 | grep -q nope"),
]
for label, cmd, n in FIRE:
    got = write_steps_after_and(cmd)
    check(f"fire: {label}", len(got) == n, f"got={got}")
for label, cmd in SILENT:
    got = write_steps_after_and(cmd)
    check(f"silent: {label}", not got, f"got={got}")


def run(payload):
    res = subprocess.run([sys.executable, str(HOOK)], input=payload if isinstance(payload, str) else json.dumps(payload),
                         capture_output=True, text=True)
    return res.returncode, (json.loads(res.stdout) if res.stdout.strip() else None)


rc, out = run({"hook_event_name": "PostToolUseFailure", "tool_name": "Bash", "tool_use_id": "t1",
               "tool_input": {"command": FIRE[0][1]}, "error": "Exit code 128\nfatal: pathspec 'hooks'",
               "is_interrupt": False})
ctx = (out or {}).get("hookSpecificOutput", {}).get("additionalContext", "")
check("e2e: note names the skipped steps", rc == 0 and "git commit" in ctx and "git push" in ctx, f"rc={rc} out={out}")
check("e2e: event name is PostToolUseFailure", (out or {}).get("hookSpecificOutput", {}).get("hookEventName") == "PostToolUseFailure")
rc, out = run({"tool_name": "Bash", "tool_input": {"command": FIRE[0][1]}, "is_interrupt": True})
check("e2e: interrupted call stays silent", rc == 0 and out is None)
rc, out = run({"tool_name": "Bash", "tool_input": {"command": "make; cp a b"}})
check("e2e: nothing to say -> silent", rc == 0 and out is None)
rc, out = run("not json")
check("e2e: malformed input fails open silently", rc == 0 and out is None)

print("ALL OK")
