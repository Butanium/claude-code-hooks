#!/usr/bin/env python3
"""session_env.py: what it writes to $CLAUDE_ENV_FILE, and that it prints nothing.

The live end-to-end (a tmux-launched `claude -p` with a stale inherited key ends
up with the file's value in Bash) is tests/smoke_session_env.sh.

Run: python3 tests/test_session_env.py
"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "session_env.py"


def check(label, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        sys.exit(1)


tmp = Path(tempfile.mkdtemp())
secrets = tmp / "fake secrets's file"  # a quote in the path, on purpose
secrets.write_text("# comment\nexport FAKE_SWEEP_KEY=fresh-value\nNOT_EXPORTED_FAKE=local\necho should-not-print\n")
env_file = tmp / "envfile.sh"


def run(**env):
    base = {k: v for k, v in os.environ.items() if k not in ("CLAUDE_SECRETS_FILE", "CLAUDE_HOOKS_KEEP_CC_SEARCH")}
    res = subprocess.run([sys.executable, str(HOOK)], input='{"hook_event_name":"SessionStart","source":"startup"}',
                         capture_output=True, text=True, env={**base, "CLAUDE_ENV_FILE": str(env_file), **env})
    return res


res = run(CLAUDE_SECRETS_FILE=str(secrets))
check("exit 0, prints nothing (SessionStart stdout would land in context)", res.returncode == 0 and res.stdout == "" and res.stderr == "")
content = env_file.read_text()
check("unsets the CLI's grep/find functions", "unset -f grep find" in content)
check("sources the secrets file", str(secrets).replace("'", "'\\''") in content)
check("never writes a value", "fresh-value" not in content)
run(CLAUDE_SECRETS_FILE=str(secrets))
check("idempotent on a second SessionStart (resume/compact)", env_file.read_text() == content)

sh = subprocess.run(["bash", "-c", f'export FAKE_SWEEP_KEY=stale-value; . "{env_file}"; '
                     'echo "key=$FAKE_SWEEP_KEY"; env | grep -c "^NOT_EXPORTED_FAKE=" || true'],
                    capture_output=True, text=True)
check("a stale inherited key takes the file's value", "key=fresh-value" in sh.stdout, sh.stdout + sh.stderr)
check("sourcing prints nothing of the file's own output", "should-not-print" not in sh.stdout)
check("a non-exported assignment stays unexported", sh.stdout.strip().endswith("0"), sh.stdout)

env_file.unlink()
run(CLAUDE_SECRETS_FILE=str(tmp / "missing"))
check("missing secrets file: only the unset line", env_file.read_text().strip() == "unset -f grep find 2>/dev/null")
env_file.unlink()
run(CLAUDE_HOOKS_KEEP_CC_SEARCH="1")
check("opt-out and no secrets: nothing written", not env_file.exists())
res = subprocess.run([sys.executable, str(HOOK)], input="", capture_output=True, text=True,
                     env={k: v for k, v in os.environ.items() if k != "CLAUDE_ENV_FILE"})
check("no CLAUDE_ENV_FILE: silent no-op", res.returncode == 0 and res.stdout == "")

print("ALL OK")
