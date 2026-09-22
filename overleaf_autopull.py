#!/usr/bin/env python3
"""UserPromptSubmit hook: pull the repo before each prompt, for projects that
someone else edits at the same time (typically an Overleaf git mirror).

Opt-in per project: runs only when CLAUDE_IS_OVERLEAF_PROJECT=true, which goes
in that project's `.claude/settings.local.json` `env` block.

Never blocks the prompt, and never leaves the repo in a different state than a
clean pull would: a fast-forward, or a rebase of local commits when the tree is
clean. Anything else (local edits in the way, a rebase conflict, a failed
fetch) is left untouched and reported to both Claude and the user. A
successful pull adds one line of context naming the files that changed.
"""
import json
import os
import subprocess
import sys

ENV_VAR = "CLAUDE_IS_OVERLEAF_PROJECT"
GIT_TIMEOUT = 30


class GitError(Exception):
    pass


def git(cwd: str, *args: str) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    try:
        done = subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise GitError(f"`git {' '.join(args)}` timed out after {GIT_TIMEOUT}s")
    if done.returncode != 0:
        raise GitError(f"`git {' '.join(args)}` failed:\n{(done.stdout + done.stderr).strip()}")
    return done.stdout.strip()


def emit(context: str, user_message: str | None = None) -> None:
    out: dict = {
        "hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}
    }
    if user_message:
        out["systemMessage"] = user_message
    print(json.dumps(out))


def fail(reason: str) -> None:
    emit(
        f"Auto-pull ({ENV_VAR}) did not pull; the repo is unchanged. {reason}\n"
        "The collaborator's latest edits are not in the working tree. Resolve this "
        "(commit/push or rebase your local work) before editing files they may have touched.",
        user_message=f"Auto-pull skipped: {reason.splitlines()[0]}",
    )


def pull(cwd: str) -> None:
    try:
        upstream = git(cwd, "rev-parse", "--abbrev-ref", "@{u}")
    except GitError as e:
        fail(f"No upstream branch to pull from.\n{e}")
        return
    try:
        git(cwd, "fetch", "--quiet")
    except GitError as e:
        fail(str(e))
        return

    behind = int(git(cwd, "rev-list", "--count", "HEAD..@{u}"))
    if behind == 0:
        return
    ahead = int(git(cwd, "rev-list", "--count", "@{u}..HEAD"))
    changed = git(cwd, "diff", "--name-only", "HEAD...@{u}").splitlines()
    changed_str = ", ".join(changed) or "(no file changes)"

    if ahead == 0:
        try:
            git(cwd, "merge", "--ff-only", "@{u}")
        except GitError as e:
            fail(f"Uncommitted local changes overlap the incoming ones.\n{e}")
            return
        emit(f"Auto-pull: fast-forwarded {behind} commit(s) from {upstream}. Changed upstream: {changed_str}.")
        return

    if git(cwd, "status", "--porcelain", "--untracked-files=no"):
        fail(
            f"{ahead} local commit(s) and {behind} upstream commit(s) have diverged, and "
            "there are uncommitted changes, so no rebase was attempted."
        )
        return
    try:
        git(cwd, "rebase", "@{u}")
    except GitError as e:
        conflicted = git(cwd, "diff", "--name-only", "--diff-filter=U").splitlines()
        subprocess.run(["git", "rebase", "--abort"], cwd=cwd, capture_output=True, check=False)
        fail(
            f"Rebasing {ahead} local commit(s) onto {behind} upstream commit(s) conflicted "
            f"in: {', '.join(conflicted) or '(unknown files)'}. The rebase was aborted.\n{e}"
        )
        return
    emit(
        f"Auto-pull: rebased {ahead} local commit(s) onto {behind} new commit(s) from "
        f"{upstream} (not pushed). Changed upstream: {changed_str}."
    )


def main() -> None:
    if os.environ.get(ENV_VAR, "").strip().lower() not in ("1", "true", "yes"):
        return
    data = json.load(sys.stdin)
    cwd = data.get("cwd") or os.getcwd()
    try:
        pull(cwd)
    except GitError as e:
        fail(str(e))


if __name__ == "__main__":
    main()
