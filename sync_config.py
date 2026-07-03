#!/usr/bin/env python3
"""Pull latest .claude config from git on session start.

Output goes to ~/.claude/debug/sync.log. On failure, log is printed to stdout.
"""

import subprocess
import os
import sys
from pathlib import Path

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
LOG_FILE = CLAUDE_DIR / "debug" / "sync.log"


def sync_config() -> tuple[bool, str]:
    """
    Pull latest config from git.

    Returns (success, message).
    """
    git_dir = CLAUDE_DIR / ".git"
    if not git_dir.is_dir():
        return False, "~/.claude is not a git repo - run: cd ~/.claude && git init && git remote add origin <your-repo>"

    try:
        result = subprocess.run(
            ["git", "-C", str(CLAUDE_DIR), "pull", "--ff-only"],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode == 0:
            # Submodules (e.g. scripts/cli-patches) aren't touched by pull;
            # init/update them so a fresh machine gets a working checkout.
            sub = subprocess.run(
                ["git", "-C", str(CLAUDE_DIR), "submodule", "update", "--init", "--recursive"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if sub.returncode != 0:
                return False, f"git submodule update failed: {sub.stderr.strip()}"
            if "Already up to date" in result.stdout:
                return True, "Already up to date"
            return True, f"Synced: {result.stdout.strip()}"
        return False, f"git pull failed: {result.stderr.strip()}"
    except subprocess.TimeoutExpired:
        return False, "git pull/submodule update timed out"
    except Exception as e:
        return False, f"sync error: {e}"


def main():
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    success, message = sync_config()

    with open(LOG_FILE, "w") as f:
        f.write(message + "\n")

    if not success:
        print(message)
        sys.exit(1)


if __name__ in ["__main__", "<run_path>"]:
    main()
