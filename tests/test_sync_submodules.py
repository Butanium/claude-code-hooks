#!/usr/bin/env python3
"""Regression tests for sync_config.sync_submodules.

The bug being pinned: `git submodule update` checks out the superproject's recorded
commit and detaches HEAD, so a submodule holding commits the gitlink doesn't reach
loses them from view — and because committing inside a submodule leaves you detached
by default, there may be no branch ref to recover from. The sync hook runs at session
start, unwatched, which is the worst possible place for that.

Run: python3 tests/test_sync_submodules.py
"""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="synctest-"))
os.environ["CLAUDE_CONFIG_DIR"] = str(TMP / "super")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sync_config  # noqa: E402  (must follow the env var)


def run(cwd, *args, check=True):
    # file:// submodules are refused by default since the CVE-2022-39253 fix
    res = subprocess.run(
        ["git", "-c", "protocol.file.allow=always", "-C", str(cwd), *args],
        capture_output=True, text=True,
    )
    if check and res.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed in {cwd}:\n{res.stderr}")
    return res.stdout.strip()


def commit(repo, name, text):
    (repo / name).write_text(text)
    run(repo, "add", name)
    run(repo, "commit", "-m", f"add {name}")
    return run(repo, "rev-parse", "HEAD")


def build():
    """A superproject pinning `sub` at commit B, where sub also has a later commit C."""
    upstream = TMP / "sub-origin"
    upstream.mkdir(parents=True)
    run(upstream, "init", "-q", "-b", "main")
    run(upstream, "config", "user.email", "t@t"); run(upstream, "config", "user.name", "t")
    a = commit(upstream, "a.txt", "A")
    b = commit(upstream, "b.txt", "B")

    super_ = TMP / "super"
    super_.mkdir(parents=True)
    run(super_, "init", "-q", "-b", "main")
    run(super_, "config", "user.email", "t@t"); run(super_, "config", "user.name", "t")
    (super_ / "readme").write_text("x")
    run(super_, "add", "readme")
    run(super_, "commit", "-m", "init")
    run(super_, "submodule", "add", str(upstream), "sub")
    run(super_, "commit", "-m", "add submodule")  # gitlink pinned at B
    sub = super_ / "sub"
    run(sub, "config", "user.email", "t@t"); run(sub, "config", "user.name", "t")
    return super_, sub, a, b


def check(label, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        sys.exit(1)


super_, sub, A, B = build()

# --- 1. submodule AHEAD of the gitlink must not be moved -----------------------
run(sub, "checkout", "-q", "-b", "work")
C = commit(sub, "c.txt", "C")
ok, notes, warns = sync_config.sync_submodules()
check("ahead: sync reports success", ok)
check("ahead: submodule left at its own commit", run(sub, "rev-parse", "HEAD") == C,
      f"HEAD moved to {run(sub, 'rev-parse', 'HEAD')[:10]}, expected {C[:10]}")
check("ahead: warned about it", any("AHEAD" in w for w in warns), f"warnings={warns}")
check("ahead: warning names the submodule", any("sub" in w for w in warns))
check("ahead: not listed as updated", not any("sub" in n for n in notes), f"notes={notes}")

# --- 2. submodule strictly BEHIND is fast-forwarded ----------------------------
run(sub, "checkout", "-q", A)
ok, notes, warns = sync_config.sync_submodules()
check("behind: sync reports success", ok)
check("behind: submodule advanced to the pinned commit", run(sub, "rev-parse", "HEAD") == B,
      f"HEAD={run(sub, 'rev-parse', 'HEAD')[:10]}, pinned={B[:10]}")
check("behind: no warning", not warns, f"warnings={warns}")

# --- 3. uncommitted changes are never checked out over ------------------------
run(sub, "checkout", "-q", A)
(sub / "a.txt").write_text("locally modified")
ok, notes, warns = sync_config.sync_submodules()
check("dirty: sync reports success", ok)
check("dirty: submodule not moved", run(sub, "rev-parse", "HEAD") == A)
check("dirty: local edit survives", (sub / "a.txt").read_text() == "locally modified")
check("dirty: warned about it", any("uncommitted" in w for w in warns), f"warnings={warns}")

# --- 4. a submodule already in sync is a no-op --------------------------------
run(sub, "checkout", "-q", "--", "a.txt")
run(sub, "checkout", "-q", B)
ok, notes, warns = sync_config.sync_submodules()
check("in-sync: no warnings, nothing updated", ok and not warns and not notes,
      f"notes={notes} warnings={warns}")

print("ALL OK")
subprocess.run(["rm", "-rf", str(TMP)])
