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

from utils._encoding import utf8_stdio

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
        encoding="utf-8",
        errors="replace",
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


def direct_submodules() -> set[str]:
    """Paths of the superproject's own submodules (not nested ones).

    A nested submodule's gitlink lives in its parent submodule's index, not
    here, so `git add` from the superproject cannot bump it.
    """
    res = git("submodule", "status", timeout=15)
    if res.returncode != 0:
        return set()
    out = set()
    for line in res.stdout.splitlines():
        parsed = parse_submodule_status(line)
        if parsed:
            out.add(parsed[1])
    return out


def bump_submodule_pointers(paths: list[str]) -> str:
    """Fast-forward the superproject's gitlinks onto the submodules' HEADs.

    This is the opposite move to `git submodule update`: instead of dragging the
    submodule back to the recorded commit, drag the recorded commit forward to
    what the submodule already has. Nothing can be stranded by it — the commits
    stay exactly where they are and simply become reachable from the gitlink.
    Safe to automate only for commits that are already on a remote, which is the
    caller's job to check.

    Explicit pathspec, like `commit_journals`, so nothing else the user has
    staged rides along.
    """
    add = git("add", "--", *paths)
    if add.returncode != 0:
        return f"submodule bump add failed: {add.stderr.strip()}"
    commit = git(
        "commit",
        "-m",
        f"submodules: bump {', '.join(paths)} ({socket.gethostname()})",
        "--",
        *paths,
    )
    if commit.returncode != 0:
        return f"submodule bump commit failed: {commit.stderr.strip()}"
    return f"submodule pointer bumped: {', '.join(paths)}"


def git_in(path: Path, *args: str, timeout: int = 15) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def parse_submodule_status(line: str) -> tuple[str, str] | None:
    """(status prefix, path) from one `git submodule status` line.

    Format is `<prefix><sha> <path> (<describe>)`, where prefix is ' ' in sync,
    '+' checked-out commit differs from the recorded one, '-' not initialized,
    'U' merge conflicts. The describe suffix is absent for uninitialized ones.
    """
    if not line.strip():
        return None
    prefix, rest = line[0], line[1:]
    fields = rest.split(None, 1)
    if len(fields) < 2:
        return None
    path = fields[1].strip()
    if path.endswith(")") and " (" in path:
        path = path.rsplit(" (", 1)[0].strip()
    return prefix, path


def recorded_sha(path: str) -> str | None:
    """The commit the superproject pins this submodule to."""
    res = git("ls-tree", "HEAD", "--", path)
    if res.returncode != 0 or not res.stdout.strip():
        return None
    fields = res.stdout.split()
    return fields[2] if len(fields) > 2 else None


def sync_submodules() -> tuple[bool, list[str], list[str]]:
    """Init/update submodules, but never move one that carries local commits.

    `git submodule update` checks out the superproject's recorded commit and detaches
    HEAD. When the submodule holds commits the gitlink doesn't reach — someone committed
    there and hasn't bumped the pointer yet — that checkout strands the work where only
    the reflog can find it. Committing inside a submodule leaves you on a detached HEAD
    by default, so there isn't even a branch ref to recover from, and this runs at
    session start where nobody is watching.

    But "ahead" has two cases, and only one of them is a problem. When the submodule's
    commits are already on its remote, the fix is not to move the submodule back — it is
    to move the gitlink forward, which strands nothing and publishes nothing new. That
    case is now bumped automatically rather than reported, because the report was pure
    nagging: it asked for two commands whose safety this function had just finished
    establishing. Unpushed commits still get the warning, since bumping would leave the
    superproject pointing at a commit no other clone can fetch, and pushing them from a
    session-start hook would publish unreviewed work to repos that are public.

    So: initialize what's missing, fast-forward what is merely behind, bump the pointer
    for what is ahead-and-published, and skip everything else loudly.
    """
    status = git("submodule", "status", "--recursive", timeout=30)
    if status.returncode != 0:
        return False, [], [f"git submodule status failed: {status.stderr.strip()}"]

    to_update, to_bump, warnings, notes = [], [], [], []
    direct = direct_submodules()
    for line in status.stdout.splitlines():
        parsed = parse_submodule_status(line)
        if parsed is None:
            continue
        prefix, path = parsed
        sub = CLAUDE_DIR / path
        if prefix == "-":
            to_update.append(path)  # not initialized: nothing local to lose
            continue
        if prefix == "U":
            warnings.append(f"submodule {path}: merge conflicts — skipped, resolve manually")
            continue
        if prefix != "+":
            continue  # already at the recorded commit

        want = recorded_sha(path)
        have = git_in(sub, "rev-parse", "HEAD").stdout.strip()
        if not want or not have:
            # A nested submodule is pinned by its PARENT's tree, not ours, so
            # `ls-tree HEAD -- <path>` finds nothing here. That is not the same
            # as an unreadable repo, and saying so saves the next reader the
            # detour of checking whether the submodule is broken.
            why = (
                "pinned by its parent submodule, not by this repo — bump it from there"
                if path not in direct
                else "cannot read commits"
            )
            warnings.append(f"submodule {path}: {why} — skipped")
            continue
        # the pinned commit can be unknown locally when the pointer moved on another
        # machine; fetch before concluding anything about ancestry
        if git_in(sub, "cat-file", "-e", f"{want}^{{commit}}").returncode != 0:
            git_in(sub, "fetch", "--quiet", timeout=30)
        if git_in(sub, "cat-file", "-e", f"{want}^{{commit}}").returncode != 0:
            warnings.append(f"submodule {path}: pinned commit {want[:10]} not found — skipped")
            continue
        ahead = git_in(sub, "merge-base", "--is-ancestor", want, have).returncode == 0
        behind = git_in(sub, "merge-base", "--is-ancestor", have, want).returncode == 0
        if ahead:
            n = git_in(sub, "rev-list", "--count", f"{want}..{have}").stdout.strip() or "?"
            # Already on a remote => bumping the gitlink publishes nothing new and
            # can strand nothing, so do it instead of nagging. Unpushed commits are
            # a different story: the superproject would then point at a commit no
            # other clone can fetch, and pushing them here would publish unreviewed
            # work (several of these submodules are public repos).
            unpushed = git_in(sub, "rev-list", "--count", "HEAD", "--not", "--remotes").stdout.strip()
            if unpushed == "0" and path in direct:
                to_bump.append(path)
                continue
            warnings.append(
                f"submodule {path}: HEAD is {n} commit(s) AHEAD of the pinned "
                f"{want[:10]}, {unpushed} of them unpushed — NOT updated (it would "
                f"strand that work), and the pointer was not bumped either (it would "
                f"point at a commit no other clone can fetch). "
                f"Push them and bump the pointer: git -C {path} push && git add {path}"
            )
            continue
        if not behind:
            warnings.append(
                f"submodule {path}: HEAD {have[:10]} has diverged from pinned "
                f"{want[:10]} — NOT updated, reconcile manually"
            )
            continue
        if git_in(sub, "status", "--porcelain").stdout.strip():
            warnings.append(
                f"submodule {path}: uncommitted changes — NOT updated "
                f"(would need to check out {want[:10]} over them)"
            )
            continue
        to_update.append(path)

    if to_update:
        res = git("submodule", "update", "--init", "--recursive", "--", *to_update, timeout=60)
        if res.returncode != 0:
            return False, notes, warnings + [f"git submodule update failed: {res.stderr.strip()}"]
        notes.append(f"submodules updated: {', '.join(to_update)}")
    if to_bump:
        notes.append(bump_submodule_pointers(to_bump))
    return True, notes, warnings


def commits_ahead() -> int | None:
    """Local commits not on the upstream branch; None if no upstream."""
    result = git("rev-list", "--count", "@{u}..HEAD")
    return int(result.stdout.strip()) if result.returncode == 0 else None


def ahead_behind() -> tuple[int, int] | None:
    """(ahead, behind) vs the upstream branch; None if no upstream."""
    result = git("rev-list", "--left-right", "--count", "@{u}...HEAD")
    if result.returncode != 0:
        return None
    fields = result.stdout.split()
    if len(fields) != 2:
        return None
    behind, ahead = fields
    return int(ahead), int(behind)


def sync_config() -> tuple[bool, str, list[str]]:
    """Commit journals, pull, push. Returns (success, message, warnings).

    Warnings are non-fatal but must reach the session: they name work the sync
    deliberately declined to touch.
    """
    git_dir = CLAUDE_DIR / ".git"
    if not git_dir.is_dir():
        return False, "~/.claude is not a git repo - run: cd ~/.claude && git init && git remote add origin <your-repo>", []

    try:
        parts, warnings = [], []
        journal_msg = commit_journals()
        if journal_msg:
            parts.append(journal_msg)

        # Fetch first and branch on the real ahead/behind counts, rather than
        # letting `git pull` decide. `pull --rebase` replays local commits even
        # when the remote hasn't moved, and plain rebase drops merge commits:
        # a local merge that resolved a conflict gets flattened back into the
        # conflicting commits, and the session-start hook re-hits a conflict a
        # human already settled. Nothing to pull => don't rebase.
        fetch = git("fetch", "--quiet", timeout=15)
        if fetch.returncode != 0:
            return False, "\n".join(parts + [f"git fetch failed: {fetch.stderr.strip()}"]), warnings

        counts = ahead_behind()
        ahead, behind = counts if counts else (0, 0)
        index_clean = git("diff", "--cached", "--quiet").returncode == 0

        # Rebase refuses to run on a dirty index (and --autostash would restore
        # staged changes as unstaged): if the user has something staged, stick
        # to ff-only — it succeeds unless local commits exist too, and that
        # triple overlap (local commits + staged changes + remote ahead) is for
        # a human. --rebase-merges keeps merge resolutions from being redone.
        if not behind:
            pull_args = None  # upstream already contained in HEAD
        elif ahead and index_clean:
            pull_args = ["rebase", "--rebase-merges", "--autostash", "@{u}"]
        else:
            pull_args = ["merge", "--ff-only", "@{u}"]

        result = git(*pull_args, timeout=15) if pull_args else None
        if result is not None and result.returncode != 0:
            if "rebase" in pull_args:
                # Never leave a session-start hook's mess behind: a conflicted
                # rebase would strand the repo mid-rebase with marker-riddled
                # files. Abort back to the pre-pull state and let a human (or
                # the session, loudly informed) resolve. A `*_journal.md
                # merge=union` .gitattributes entry avoids most of these.
                git("rebase", "--abort")
                return False, "\n".join(
                    parts
                    + [
                        "git rebase onto upstream conflicted (aborted — repo restored, "
                        f"local commits kept, resolve manually): {result.stderr.strip()}"
                    ]
                ), warnings
            hint = (
                " (local commits + staged changes + a moved remote — resolve manually)"
                if ahead and not index_clean
                else ""
            )
            return False, "\n".join(parts + [f"git pull failed{hint}: {result.stderr.strip()}"]), warnings
        # Submodules (e.g. scripts/cli-patches) aren't touched by pull;
        # init/update them so a fresh machine gets a working checkout.
        sub_ok, sub_notes, sub_warnings = sync_submodules()
        warnings.extend(sub_warnings)
        parts.extend(sub_notes)
        if not sub_ok:
            return False, "\n".join(parts + sub_warnings), warnings
        if result is None:
            parts.append("Already up to date")
        else:
            parts.append(f"Synced: {behind} commit(s) from upstream")

        # Push anything ahead (journal commits, or stranded commits from a
        # previous offline session) so other machines actually converge.
        if commits_ahead():
            push = git("push", timeout=15)
            if push.returncode != 0:
                return False, "\n".join(parts + [f"git push failed: {push.stderr.strip()}"]), warnings
            parts.append("Pushed local commits")

        return True, "\n".join(parts), warnings
    except subprocess.TimeoutExpired:
        return False, "git sync timed out", []
    except Exception as e:
        return False, f"sync error: {e}", []


def main():
    utf8_stdio()
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    success, message, warnings = sync_config()

    with open(LOG_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join([message, *warnings]) + "\n")

    # stdout on SessionStart lands in the session's context — that is where a skipped
    # submodule has to show up, or the sync silently looks like it did everything
    if warnings:
        print("config sync left these alone:\n" + "\n".join(f"  - {w}" for w in warnings))

    if not success:
        print(message, file=sys.stderr)
        sys.exit(1)


if __name__ in ["__main__", "<run_path>"]:
    main()