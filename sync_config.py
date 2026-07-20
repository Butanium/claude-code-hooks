#!/usr/bin/env python3
"""Sync the .claude config repo with its remote on session start.

Auto-commits journal files (`*_journal.md` anywhere in the repo), pulls, and
pushes local commits, so machines converge without anyone remembering to
commit the journals instances append to mid-session.

Output goes to ~/.claude/debug/sync.log. On failure, log is printed to stdout.
"""

import os
import socket
import subprocess
import sys
from pathlib import Path

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
LOG_FILE = CLAUDE_DIR / "debug" / "sync.log"
# git pathspec, applied from the repo root: `*` crosses directory boundaries,
# so this matches both top-level and nested (e.g. model-quirks/) journals.
JOURNAL_PATHSPEC = "*_journal.md"


def git(*args: str, timeout: int = 10) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(CLAUDE_DIR), *args],
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def commit_journals() -> str | None:
    """Commit new/modified journal files. Returns a log line, or None if clean.

    Committing with an explicit pathspec keeps anything else the user has
    staged out of the journal commit.
    """
    status = git("status", "--porcelain", "--", JOURNAL_PATHSPEC)
    if status.returncode != 0 or not status.stdout.strip():
        return None
    add = git("add", "--", JOURNAL_PATHSPEC)
    if add.returncode != 0:
        return f"journal add failed: {add.stderr.strip()}"
    files = " ".join(line[3:].strip() for line in status.stdout.strip().splitlines())
    commit = git(
        "commit",
        "-m",
        f"journal sync ({socket.gethostname()})",
        "--",
        JOURNAL_PATHSPEC,
    )
    if commit.returncode != 0:
        return f"journal commit failed: {commit.stderr.strip()}"
    return f"journal commit: {files}"


def commits_ahead() -> int | None:
    """Local commits not on the upstream branch; None if no upstream."""
    result = git("rev-list", "--count", "@{u}..HEAD")
    return int(result.stdout.strip()) if result.returncode == 0 else None


def sync_config() -> tuple[bool, str]:
    """Commit journals, pull, push. Returns (success, message)."""
    git_dir = CLAUDE_DIR / ".git"
    if not git_dir.is_dir():
        return False, "~/.claude is not a git repo - run: cd ~/.claude && git init && git remote add origin <your-repo>"

    try:
        parts = []
        journal_msg = commit_journals()
        if journal_msg:
            parts.append(journal_msg)

        # Plain ff-only pull fails whenever local commits exist (the journal
        # commit above, or an earlier session's unpushed work) — rebase local
        # commits on top of the remote in that case. Rebase refuses to run on
        # a dirty index though (and --autostash would restore staged changes
        # as unstaged): if the user has something staged, stick to ff-only —
        # it succeeds unless the remote moved too, and that triple overlap
        # (local commits + staged changes + remote ahead) is for a human.
        ahead = commits_ahead()
        index_clean = git("diff", "--cached", "--quiet").returncode == 0
        if ahead and index_clean:
            pull_args = ["pull", "--rebase", "--autostash"]
        else:
            pull_args = ["pull", "--ff-only"]
        result = git(*pull_args, timeout=15)
        if result.returncode != 0:
            if "--rebase" in pull_args:
                # Never leave a session-start hook's mess behind: a conflicted
                # rebase would strand the repo mid-rebase with marker-riddled
                # files. Abort back to the pre-pull state and let a human (or
                # the session, loudly informed) resolve. A `*_journal.md
                # merge=union` .gitattributes entry avoids most of these.
                git("rebase", "--abort")
                return False, "\n".join(
                    parts
                    + [
                        "git pull --rebase conflicted (aborted — repo restored, "
                        f"local commits kept, resolve manually): {result.stderr.strip()}"
                    ]
                )
            hint = (
                " (local commits + staged changes + a moved remote — resolve manually)"
                if ahead and not index_clean
                else ""
            )
            return False, "\n".join(parts + [f"git pull failed{hint}: {result.stderr.strip()}"])
        # Submodules (e.g. scripts/cli-patches) aren't touched by pull;
        # init/update them so a fresh machine gets a working checkout.
        sub = git("submodule", "update", "--init", "--recursive", timeout=30)
        if sub.returncode != 0:
            return False, "\n".join(parts + [f"git submodule update failed: {sub.stderr.strip()}"])
        if "Already up to date" in result.stdout:
            parts.append("Already up to date")
        else:
            parts.append(f"Synced: {result.stdout.strip()}")

        # Push anything ahead (journal commits, or stranded commits from a
        # previous offline session) so other machines actually converge.
        if commits_ahead():
            push = git("push", timeout=15)
            if push.returncode != 0:
                return False, "\n".join(parts + [f"git push failed: {push.stderr.strip()}"])
            parts.append("Pushed local commits")

        return True, "\n".join(parts)
    except subprocess.TimeoutExpired:
        return False, "git sync timed out"
    except Exception as e:
        return False, f"sync error: {e}"


def main():
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    success, message = sync_config()

    with open(LOG_FILE, "w") as f:
        f.write(message + "\n")

    if not success:
        print(message, file=sys.stderr)
        sys.exit(1)


if __name__ in ["__main__", "<run_path>"]:
    main()