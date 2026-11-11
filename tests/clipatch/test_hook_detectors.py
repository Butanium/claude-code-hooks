#!/usr/bin/env python3
"""hook-detectors: the hooks that read the claude binary for a CLI patch agree with the patch tests.

Three hooks change what they do depending on whether a patch from
claude-code-patches is in effect, and each finds out by matching bytes in the
binary:

  bgwatch_hint.py             monitor-persistent  persistent=true vs a 30-min timeout in the hint
  bgwatch_hint.py             monitor-undefer     the "ToolSearch select:Monitor first" line
  force_background_bash.py    auto-background     which commands it still has to deny
  agent_worktree_teammate.py  teammate-cwd        whether a worktree teammate starts in its worktree

A patch that gets re-anchored for a new claude version leaves these matchers
behind unless someone remembers them. From 2.1.289 to 2.1.321 the
monitor-persistent matcher did not know the patch's new shape, read every
patched binary as unknown, and bgwatch_hint told sessions "this build caps
every watch at 30 min" on binaries where persistent watches worked.

This module is loaded by the patches repo's `tests/run_tests.py`, which runs it
on the patched binary and on a copy of its `.orig`: list this directory in
CLAUDE_CLI_PATCH_EXTRA_TESTS_DIRS. To run it alone:
`python3 <patches repo>/tests/run_tests.py --binary <claude> --control --only hook-detectors`.
  patched: every detector reads the patch as in effect
  stock:   every detector reads the binary as stock
A mixed or unknown reading is INCONCLUSIVE and names the detector to re-anchor
against `patches/<name>.py` (its PATTERN / PATCHED constants).
Cross-platform.
"""

from __future__ import annotations

import sys
from pathlib import Path

HOOKS = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(HOOKS))

from _harness import INCONCLUSIVE, PATCHED, STOCK, Verdict  # noqa: E402  (patches repo, tests/)

PATCH = "hook-detectors"
COVERS = ("monitor-persistent", "monitor-undefer", "auto-background", "teammate-cwd")


def detectors():
    """(patch name, hook file, bytes -> state). States are the strings of utils/_clipatch.py,
    which are the harness's too ("patched" / "stock"); anything else is "cannot tell".
    The hooks are imported here, not at module load: a renamed hook or function then fails
    this test's row instead of stopping the suite from loading."""
    import agent_worktree_teammate
    import bgwatch_hint
    import force_background_bash

    return (
        ("monitor-persistent", "bgwatch_hint.py", bgwatch_hint._persistent_state),
        ("monitor-undefer", "bgwatch_hint.py", bgwatch_hint._undefer_state),
        ("auto-background", "force_background_bash.py", force_background_bash._auto_background_state),
        ("teammate-cwd", "agent_worktree_teammate.py",
         lambda data: PATCHED if agent_worktree_teammate._teammate_cwd_state(data) else STOCK),
    )


def run(binary: Path) -> Verdict:
    dets = detectors()
    assert {name for name, _, _ in dets} == set(COVERS)
    data = Path(binary).read_bytes()
    states = {(name, hook): detect(data) for name, hook, detect in dets}
    seen = set(states.values())
    if seen == {PATCHED}:
        return Verdict(PATCHED, f"all {len(states)} hook detectors read their patch as in effect")
    if seen == {STOCK}:
        return Verdict(STOCK, f"all {len(states)} hook detectors read the binary as stock")
    majority = max((PATCHED, STOCK), key=lambda st: sum(v == st for v in states.values()))
    odd = "; ".join(f"{hook} reads {name} as {state}" for (name, hook), state in states.items() if state != majority)
    return Verdict(INCONCLUSIVE, f"{odd} while the other detectors read {majority} "
                                 f"(re-anchor it against patches/<name>.py, or that patch is not applied here)")
