#!/usr/bin/env python3
"""PreToolUse hook: denies git commands that would discard uncommitted work,
and names the files that would go.

Unstaged changes exist in no git object. `reset --hard` over them is not
undoable by reflog, `fsck --lost-found`, or anything else -- the content is
gone. The same is true of untracked files under `clean -fd`.

The failure this guards is not ignorance of that. It is running `git status`,
reading "M some_file", and continuing anyway, because the check was run to
confirm a plan rather than to decide one. So the hook does not say "the tree is
dirty" -- the agent usually knows. It lists the specific paths this specific
command destroys, which is the sentence that has to arrive between the output
and the action.

Fires only when the command would actually destroy something: a clean tree, or
dirt that this command does not touch (untracked files under `reset --hard`,
say), passes silently.

Bypass: `[I-READ-THE-DIRTY-FILES]` in the command or the surrounding message.
Named for the claim it makes, since that claim is the thing that was false.
"""
import json
import os
import re
import shlex
import subprocess
import sys

BYPASS = "[I-READ-THE-DIRTY-FILES]"

# What each command class destroys, in `git status --porcelain` terms:
#   "tracked"   -- modifications to tracked files (staged or not)
#   "untracked" -- files git isn't tracking yet
#   "ignored"   -- additionally, ignored files
TRACKED, UNTRACKED, IGNORED = "tracked", "untracked", "ignored"

# Anchored at the start of a statement, so `echo "git reset --hard"` and
# `git log --oneline` don't match.
DESTRUCTIVE = [
    (re.compile(r"^git\s+(?:-\S+\s+|--\S+(?:=\S+)?\s+)*reset\b(?=.*\s--hard\b)"), {TRACKED},
     "git reset --hard"),
    (re.compile(r"^git\s+(?:-\S+\s+|--\S+(?:=\S+)?\s+)*checkout\b(?=.*\s(?:-f|--force)\b)"),
     {TRACKED}, "git checkout --force"),
    (re.compile(r"^git\s+(?:-\S+\s+|--\S+(?:=\S+)?\s+)*restore\b"), {TRACKED}, "git restore"),
    (re.compile(r"^git\s+(?:-\S+\s+|--\S+(?:=\S+)?\s+)*clean\b(?=.*\s-\S*[fF])"), {UNTRACKED},
     "git clean"),
]
# `git checkout -- .` / `git checkout <ref> -- <path>` overwrites the worktree
# without -f. Only the `--` form: `git checkout <branch>` refuses to clobber.
CHECKOUT_PATHSPEC = re.compile(r"^git\s+(?:-\S+\s+|--\S+(?:=\S+)?\s+)*checkout\b.*\s--\s")

# Statement separators, so only the command actually being run is inspected.
SPLIT = re.compile(r"&&|\|\||;|\n|\|")
LEADING_CD = re.compile(r"^cd\s+(\S+)\s*$")


def _classes(statement: str) -> tuple[set[str], str] | None:
    """The dirt classes `statement` would destroy, and a label for it."""
    for pattern, classes, label in DESTRUCTIVE:
        if pattern.search(statement):
            classes = set(classes)
            if label == "git clean" and re.search(r"\s-\S*x", statement):
                classes.add(IGNORED)
            return classes, label
    if CHECKOUT_PATHSPEC.search(statement):
        return {TRACKED}, "git checkout -- <path>"
    return None


def _porcelain(cwd: str, want_ignored: bool) -> list[tuple[str, str]] | None:
    """(status, path) for the repo at cwd, or None if it isn't readable."""
    cmd = ["git", "status", "--porcelain"]
    if want_ignored:
        cmd.append("--ignored")
    try:
        done = subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None  # not a repo, or git unavailable: nothing to say
    rows = []
    for line in done.stdout.splitlines():
        if len(line) > 3:
            rows.append((line[:2], line[3:].strip()))
    return rows


def _at_risk(rows: list[tuple[str, str]], classes: set[str]) -> list[str]:
    hits = []
    for status, path in rows:
        if status == "??" and UNTRACKED in classes:
            hits.append(path)
        elif status == "!!" and IGNORED in classes:
            hits.append(path)
        elif status not in ("??", "!!") and TRACKED in classes:
            hits.append(f"{path}  [{status.strip()}]")
    return hits


def _target_cwd(command: str, default: str) -> str:
    """Follow a single leading `cd <dir> &&`, the common wrapper."""
    first = SPLIT.split(command, maxsplit=1)[0].strip()
    match = LEADING_CD.match(first)
    if not match:
        return default
    try:
        path = os.path.expanduser(shlex.split(match.group(1))[0])
    except ValueError:
        return default
    if not os.path.isabs(path):
        path = os.path.join(default, path)
    return path if os.path.isdir(path) else default


def _reason(label: str, at_risk: list[str], classes: set[str]) -> str:
    shown = at_risk[:20]
    listing = "\n".join(f"    {p}" for p in shown)
    if len(at_risk) > len(shown):
        listing += f"\n    ... and {len(at_risk) - len(shown)} more"
    what = "untracked files" if classes == {UNTRACKED} else "changes"
    return (
        f"`{label}` would destroy these {what}, and they are in no commit and no "
        f"stash:\n{listing}\n\n"
        "Unstaged content exists in no git object, so this is not recoverable — not "
        "by reflog, not by `fsck --lost-found`. If you ran `git status` a moment ago "
        "and moved on, this is the line you moved past.\n\n"
        "Non-destructive ways to get the same result:\n"
        "  • `git stash push -u` first, then do the operation, then `git stash pop`\n"
        "  • `git restore --source=<ref> -- <paths>` to touch only the files you meant\n"
        "  • `git worktree add` a scratch checkout and leave this tree alone\n\n"
        f"If the listed paths really are disposable, say so with {BYPASS} in your "
        "message or as a comment in the command."
    )


def main():
    data = json.load(sys.stdin)
    if data.get("tool_name") != "Bash":
        return
    tool_input = data.get("tool_input", {})
    command = tool_input.get("command", "")
    if BYPASS in command or BYPASS in json.dumps(data.get("context", "")):
        return

    for raw in SPLIT.split(command):
        statement = raw.strip().lstrip("(").strip()
        found = _classes(statement)
        if not found:
            continue
        classes, label = found
        cwd = _target_cwd(command, data.get("cwd") or os.getcwd())
        rows = _porcelain(cwd, want_ignored=IGNORED in classes)
        if rows is None:
            return
        at_risk = _at_risk(rows, classes)
        if not at_risk:
            return  # nothing this command touches
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": _reason(label, at_risk, classes),
                    }
                }
            )
        )
        return


if __name__ == "__main__":
    main()
