"""Tests for dirty_tree_guard: it must fire on real loss and stay quiet otherwise."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "dirty_tree_guard.py"


def run_hook(command: str, cwd: Path) -> dict | None:
    payload = {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}
    done = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(done.stdout) if done.stdout.strip() else None


def denial(result: dict | None) -> str | None:
    if result is None:
        return None
    return result["hookSpecificOutput"]["permissionDecisionReason"]


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@e",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@e",
            "PATH": "/usr/bin:/bin",
            "HOME": str(repo),
        },
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "-q", "-b", "main")
    (tmp_path / "tracked.txt").write_text("v1\n")
    git(tmp_path, "add", "tracked.txt")
    git(tmp_path, "commit", "-qm", "init")
    return tmp_path


def test_reset_hard_over_a_modified_file_names_it(repo: Path) -> None:
    (repo / "tracked.txt").write_text("v2\n")
    reason = denial(run_hook("git reset --hard HEAD~1", repo))
    assert reason is not None
    assert "tracked.txt" in reason
    assert "not recoverable" in reason


def test_clean_tree_passes(repo: Path) -> None:
    assert run_hook("git reset --hard HEAD~1", repo) is None


def test_reset_hard_ignores_untracked_only_dirt(repo: Path) -> None:
    """reset --hard leaves untracked files alone, so there is nothing to warn about."""
    (repo / "new.txt").write_text("scratch\n")
    assert run_hook("git reset --hard HEAD~1", repo) is None


def test_clean_fires_on_untracked(repo: Path) -> None:
    (repo / "new.txt").write_text("scratch\n")
    reason = denial(run_hook("git clean -fd", repo))
    assert reason is not None and "new.txt" in reason


def test_clean_ignores_tracked_modifications(repo: Path) -> None:
    (repo / "tracked.txt").write_text("v2\n")
    assert run_hook("git clean -fd", repo) is None


def test_staged_changes_still_count(repo: Path) -> None:
    (repo / "tracked.txt").write_text("v2\n")
    git(repo, "add", "tracked.txt")
    assert "tracked.txt" in (denial(run_hook("git reset --hard HEAD~1", repo)) or "")


def test_bypass_tag(repo: Path) -> None:
    (repo / "tracked.txt").write_text("v2\n")
    assert run_hook("git reset --hard HEAD~1  # [I-READ-THE-DIRTY-FILES]", repo) is None


def test_mentioning_the_command_is_not_running_it(repo: Path) -> None:
    (repo / "tracked.txt").write_text("v2\n")
    assert run_hook("echo 'never git reset --hard here'", repo) is None
    assert run_hook("git log --oneline", repo) is None


def test_fires_on_a_later_statement_in_a_chain(repo: Path) -> None:
    (repo / "tracked.txt").write_text("v2\n")
    reason = denial(run_hook("git fetch origin && git reset --hard origin/main", repo))
    assert reason is not None and "tracked.txt" in reason


def test_follows_a_leading_cd(repo: Path, tmp_path: Path) -> None:
    (repo / "tracked.txt").write_text("v2\n")
    outside = tmp_path.parent / "elsewhere"
    outside.mkdir(exist_ok=True)
    reason = denial(run_hook(f"cd {repo} && git reset --hard HEAD~1", outside))
    assert reason is not None and "tracked.txt" in reason


def test_outside_a_repo_is_silent(tmp_path: Path) -> None:
    assert run_hook("git reset --hard HEAD~1", tmp_path) is None


def test_checkout_force_and_pathspec_fire(repo: Path) -> None:
    (repo / "tracked.txt").write_text("v2\n")
    assert denial(run_hook("git checkout -f main", repo)) is not None
    assert denial(run_hook("git checkout -- .", repo)) is not None


def test_plain_checkout_of_a_branch_passes(repo: Path) -> None:
    """git refuses that itself when it would clobber; no need to pre-empt it."""
    (repo / "tracked.txt").write_text("v2\n")
    assert run_hook("git checkout main", repo) is None
