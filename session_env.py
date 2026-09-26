#!/usr/bin/env python3
"""SessionStart hook: lines for $CLAUDE_ENV_FILE, which the CLI sources before
every Bash tool command of the session. Prints nothing.

1. `unset -f grep find`. When the Grep/Glob tools are off, Claude Code defines
   `grep` (its bundled ugrep, --ignore-files) and `find` (bfs) as shell
   functions in the Bash tool. They differ from GNU in ways that fail silently
   inside compound commands: gitignored directories are skipped (a research
   repo's results/), `{0,N}` context regexes exceed ugrep's complexity limit for
   N >= 20, bfs rejects `-newermt "-2 minutes"`, and `command grep` breaks under
   xargs. Unsetting them gives the system tools back; verified with fresh
   `claude -p` sessions on 2.1.280 (GNU grep 3.12 on three separate calls).
   `CLAUDE_HOOKS_KEEP_CC_SEARCH=1` skips this.

2. Re-source the secrets file named by `$CLAUDE_SECRETS_FILE` (~ and $VARS
   expanded; unset = skip). A tmux server keeps the environment it started
   with, so sessions and teammates it launches can carry months-old keys (one
   nearly billed the wrong account) or lack newer variables. Sourcing the file
   at every Bash call gives the file's current values. The hook never reads the
   values: it writes a `.` line, so only what the file itself exports is
   exported, exactly as a login shell's `. ~/.secrets` would. Hooks and the CLI
   process itself still see the inherited environment.

Fail-open: any error exits 0 without output.
"""
import os
import sys
from pathlib import Path


def lines() -> list[str]:
    out = []
    if os.environ.get("CLAUDE_HOOKS_KEEP_CC_SEARCH") != "1":
        out.append("unset -f grep find 2>/dev/null")
    secrets = os.environ.get("CLAUDE_SECRETS_FILE")
    if secrets:
        path = Path(os.path.expandvars(os.path.expanduser(secrets)))
        if path.is_file():
            quoted = "'" + str(path).replace("'", "'\\''") + "'"
            out.append(f"[ -r {quoted} ] && . {quoted} >/dev/null 2>&1")
    return out


def main():
    env_file = os.environ.get("CLAUDE_ENV_FILE")
    if not env_file:
        return
    new = lines()
    if not new:
        return
    path = Path(env_file)
    existing = path.read_text().splitlines() if path.exists() else []
    with path.open("a") as f:
        for line in new:
            if line not in existing:  # resume/compact re-fire SessionStart on the same file
                f.write(line + "\n")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
