"""Tests for overleaf_autopull: pull when safe, otherwise leave the repo untouched and say why."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "overleaf_autopull.py"
GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@e",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@e",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "PATH": "/usr/bin:/bin",
}


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True,
        env={**GIT_ENV, "HOME": str(repo)},
    )
    return done.stdout.strip()


def run_hook(cwd: Path, enabled: bool = True) -> dict | None:
    env = {**GIT_ENV, "HOME": str(cwd)}
    if enabled:
        env["CLAUDE_IS_OVERLEAF_PROJECT"] = "true"
    done = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"cwd": str(cwd), "prompt": "hi"}),
        capture_output=True, text=True, check=True, env=env,
    )
    return json.loads(done.stdout) if done.stdout.strip() else None


def context(result: dict | None) -> str:
    assert result is not None
    return result["hookSpecificOutput"]["additionalContext"]


def commit(repo: Path, name: str, content: str) -> None:
    (repo / name).write_text(content)
    git(repo, "add", name)
    git(repo, "commit", "-qm", f"edit {name}")


@pytest.fixture
def clones(tmp_path: Path) -> tuple[Path, Path]:
    """(local, other): two clones of one bare remote, both at the same commit."""
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    local, other = tmp_path / "local", tmp_path / "other"
    git(tmp_path, "clone", "-q", str(remote), str(local))
    commit(local, "main.tex", "a\nb\nc\n")
    commit(local, "other.tex", "x\n")
    git(local, "push", "-q", "origin", "main")
    git(tmp_path, "clone", "-q", str(remote), str(other))
    return local, other


def push_from(other: Path, name: str, content: str) -> None:
    commit(other, name, content)
    git(other, "push", "-q", "origin", "main")


def test_disabled_does_nothing(clones):
    local, other = clones
    push_from(other, "main.tex", "a\nB\nc\n")
    head = git(local, "rev-parse", "HEAD")
    assert run_hook(local, enabled=False) is None
    assert git(local, "rev-parse", "HEAD") == head


def test_up_to_date_is_silent(clones):
    local, _ = clones
    assert run_hook(local) is None


def test_fast_forward_names_changed_files(clones):
    local, other = clones
    push_from(other, "main.tex", "a\nB\nc\n")
    ctx = context(run_hook(local))
    assert "fast-forwarded 1 commit" in ctx and "main.tex" in ctx
    assert (local / "main.tex").read_text() == "a\nB\nc\n"


def test_dirty_file_not_touched_upstream_still_fast_forwards(clones):
    local, other = clones
    push_from(other, "main.tex", "a\nB\nc\n")
    (local / "other.tex").write_text("local edit\n")
    assert "fast-forwarded" in context(run_hook(local))
    assert (local / "other.tex").read_text() == "local edit\n"


def test_dirty_overlap_leaves_everything_as_is(clones):
    local, other = clones
    push_from(other, "main.tex", "a\nB\nc\n")
    (local / "main.tex").write_text("a\nb\nC\n")
    head = git(local, "rev-parse", "HEAD")
    result = run_hook(local)
    assert "did not pull" in context(result) and "systemMessage" in result
    assert git(local, "rev-parse", "HEAD") == head
    assert (local / "main.tex").read_text() == "a\nb\nC\n"


def test_diverged_without_conflict_rebases(clones):
    local, other = clones
    push_from(other, "main.tex", "a\nB\nc\n")
    commit(local, "other.tex", "y\n")
    ctx = context(run_hook(local))
    assert "rebased 1 local commit(s) onto 1" in ctx
    assert (local / "main.tex").read_text() == "a\nB\nc\n"
    assert (local / "other.tex").read_text() == "y\n"


def test_diverged_with_conflict_aborts_and_names_file(clones):
    local, other = clones
    push_from(other, "main.tex", "a\nREMOTE\nc\n")
    commit(local, "main.tex", "a\nLOCAL\nc\n")
    head = git(local, "rev-parse", "HEAD")
    ctx = context(run_hook(local))
    assert "conflicted in: main.tex" in ctx and "aborted" in ctx
    assert git(local, "rev-parse", "HEAD") == head
    assert not (local / ".git" / "rebase-merge").exists()
    assert (local / "main.tex").read_text() == "a\nLOCAL\nc\n"


def test_diverged_and_dirty_does_not_rebase(clones):
    local, other = clones
    push_from(other, "main.tex", "a\nB\nc\n")
    commit(local, "other.tex", "y\n")
    (local / "other.tex").write_text("dirty\n")
    head = git(local, "rev-parse", "HEAD")
    assert "no rebase was attempted" in context(run_hook(local))
    assert git(local, "rev-parse", "HEAD") == head


def test_no_upstream_is_reported(tmp_path):
    git(tmp_path, "init", "-q", "-b", "main")
    commit(tmp_path, "f", "1\n")
    assert "No upstream" in context(run_hook(tmp_path))
