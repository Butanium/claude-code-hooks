#!/usr/bin/env python3
"""PreToolUse hook (Agent): `name` + `isolation:"worktree"` -> a pane teammate in a fresh worktree.

Stock CLI sends any Agent call carrying `isolation` down the in-process subagent
path, even with `name` set, so asking for "a teammate in its own worktree" got an
in-process background subagent instead of a tmux teammate. The `teammate-cwd`
patch (claude-code-patches) lets a named call carry `cwd` and hands it to the pane
spawner, which launches the teammate as `cd <cwd> && claude ...`. This hook does
the worktree part: it runs `git worktree add` from the lead's current directory
and rewrites the call to `cwd: <worktree>` without `isolation`.

It only rewrites when the claude process that fired it runs the patch. That is
checked on the running image (`/proc/<pid>/exe`), not the file on disk: a lead
started before the patch landed keeps executing stock code, where the rewrite
would give a teammate in the lead's own directory. Otherwise the call passes
through and gets the stock behavior.

`force_background_task.py` skips this class of call (two hooks returning
`updatedInput` for one call race, and the last result wins), so this hook also
sets `run_in_background`.

Forks never become teammates, so `subagent_type: "fork"` is left alone.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(__file__))
from utils._clipatch import module_runs_from_source  # noqa: E402

MARKER = b"/*T7cwd"  # kept in sync with patches/teammate-cwd.py in claude-code-patches


def is_candidate(tool_input: dict) -> bool:
    return (
        bool(tool_input.get("name"))
        and tool_input.get("isolation") == "worktree"
        and tool_input.get("subagent_type") != "fork"
        and not tool_input.get("cwd")
    )


def _claude_ancestor_exe() -> str | None:
    """`/proc/<pid>/exe` of the nearest ancestor that is a claude binary."""
    override = os.environ.get("CLAUDE_HOOKS_CLAUDE_BIN")
    if override:
        return override
    pid = os.getppid()
    for _ in range(8):
        if pid <= 1:
            return None
        try:
            target = os.readlink(f"/proc/{pid}/exe")
            with open(f"/proc/{pid}/stat") as f:
                ppid = int(f.read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            return None
        name = Path(target.removesuffix(" (deleted)")).name
        if name.startswith("claude") or "/claude/versions/" in target:
            return f"/proc/{pid}/exe"
        pid = ppid
    return None


def _teammate_mode(argv: list[str]) -> str:
    """--teammate-mode flag > settings.json > ~/.claude.json > "auto"."""
    for i, a in enumerate(argv):
        if a == "--teammate-mode" and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith("--teammate-mode="):
            return a.split("=", 1)[1]
    config = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
    for f in (config / "settings.local.json", config / "settings.json", Path.home() / ".claude.json"):
        try:
            mode = json.loads(f.read_text()).get("teammateMode")
        except (OSError, ValueError, AttributeError):
            continue
        if mode:
            return mode
    return "auto"


def teammates_run_in_process() -> bool:
    """Mirror of the CLI's isInProcessEnabled (BackendRegistry): non-interactive
    sessions, mode "in-process", or mode "auto" outside tmux/iTerm2. The runtime
    fallback after a pane backend fails can't be seen from here; the patched
    in-process spawner refuses `cwd` for that case."""
    exe = _claude_ancestor_exe()
    argv: list[str] = []
    if exe and exe.startswith("/proc/"):
        try:
            argv = Path(exe).with_name("cmdline").read_bytes().decode(errors="replace").split("\0")
        except OSError:
            pass
    if "-p" in argv or "--print" in argv:
        return True
    mode = _teammate_mode(argv)
    if mode == "in-process":
        return True
    if mode in ("tmux", "iterm2"):
        return False
    return not os.environ.get("TMUX") and os.environ.get("TERM_PROGRAM") != "iTerm.app"


def running_binary_patched() -> bool:
    exe = _claude_ancestor_exe()
    if not exe:
        return False
    try:
        st = os.stat(exe)  # follows /proc/<pid>/exe to the running inode, deleted or not
    except OSError:
        return False
    key = [st.st_dev, st.st_ino, st.st_size, int(st.st_mtime)]
    cache = Path(tempfile.gettempdir()) / f"clipatch_teammate_cwd_{os.getuid()}.json"
    try:
        c = json.loads(cache.read_text())
        if c.get("key") == key:
            return bool(c.get("value"))
    except (OSError, ValueError):
        pass
    try:
        data = Path(exe).read_bytes()
    except OSError:
        return False
    at = data.find(MARKER)
    value = at != -1 and module_runs_from_source(data, at)
    try:
        tmp = cache.with_suffix(f".{os.getpid()}")
        tmp.write_text(json.dumps({"key": key, "value": value}))
        os.replace(tmp, cache)
    except OSError:
        pass
    return value


def _git(cwd: str, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", cwd, *args], capture_output=True, text=True, check=True, timeout=60
    ).stdout.strip()


def create_worktree(lead_cwd: str, name: str) -> tuple[str, str]:
    """`git worktree add` a new branch off the lead's HEAD; return (path, branch).

    Worktrees go under the main checkout's `.claude/worktrees/` (the directory the
    stock `isolation:"worktree"` uses), even when the lead itself sits in a
    worktree, so they don't nest.
    """
    common = Path(_git(lead_cwd, "rev-parse", "--path-format=absolute", "--git-common-dir"))
    root = common.parent
    base = root / ".claude" / "worktrees"
    for i in range(1, 50):
        suffix = "" if i == 1 else f"-{i}"
        path, branch = base / f"{name}{suffix}", f"worktree-{name}{suffix}"
        branch_exists = subprocess.run(
            ["git", "-C", lead_cwd, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"]
        ).returncode == 0
        if not path.exists() and not branch_exists:
            break
    else:
        raise RuntimeError(f"no free worktree slot for {name!r} under {base}")
    _git(lead_cwd, "worktree", "add", "-b", branch, str(path), "HEAD")
    exclude = common / "info" / "exclude"
    ignored = subprocess.run(["git", "-C", str(root), "check-ignore", "-q", str(path)]).returncode == 0
    if not ignored:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with exclude.open("a") as f:
            f.write("\n/.claude/worktrees/\n")
    return str(path), branch


def rewrite(tool_input: dict, path: str, branch: str) -> dict:
    new = {k: v for k, v in tool_input.items() if k != "isolation"}
    new["cwd"] = path
    new["run_in_background"] = True
    new["prompt"] = (
        f"(You are working in a dedicated git worktree: {path}, branch {branch}, "
        f"created from the lead's checkout at HEAD. Commit your work on that branch.)\n\n"
        + tool_input.get("prompt", "")
    )
    return new


def emit(tool_input: dict, context: str) -> None:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "updatedInput": tool_input,
        "additionalContext": context,
    }}))


def main() -> int:
    data = json.load(sys.stdin)
    if data.get("tool_name") not in ("Agent", "Task"):
        return 0
    ti = data.get("tool_input", {})
    if not is_candidate(ti):
        return 0

    reason = None
    if not running_binary_patched():
        reason = "this claude process doesn't run the teammate-cwd CLI patch"
    elif teammates_run_in_process():
        reason = "teammates run in-process in this session, and those can't take a cwd"
    else:
        try:
            path, branch = create_worktree(data.get("cwd") or os.getcwd(), ti["name"])
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, RuntimeError, OSError) as e:
            detail = getattr(e, "stderr", None) or str(e)
            reason = f"creating the worktree failed: {detail.strip()}"

    if reason is None:
        emit(rewrite(ti, path, branch),
             f"agent_worktree_teammate hook: created git worktree {path} (branch {branch}) and "
             f"spawned '{ti['name']}' there as a pane teammate instead of an in-process subagent. "
             f"The worktree is not removed automatically: merge the branch, then "
             f"`git worktree remove {path}`.")
        return 0

    # Pass through to stock behavior (in-process subagent in a stock worktree),
    # keeping the background default force_background_task.py would have applied.
    if not ti.get("run_in_background"):
        emit({**ti, "run_in_background": True},
             f"agent_worktree_teammate hook: '{ti['name']}' will run as an in-process subagent "
             f"in a stock worktree, not as a pane teammate ({reason}).")
    else:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": f"agent_worktree_teammate hook: '{ti['name']}' will run as an "
                                 f"in-process subagent, not as a pane teammate ({reason}).",
        }}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
