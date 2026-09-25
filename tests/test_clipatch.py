#!/usr/bin/env python3
"""Paired assertions for the CLI-patch detectors in utils/_clipatch.py.

A detector that answers only "patched / not patched" cannot tell "the binary is
unpatched" from "my anchor moved and I can no longer look", and silently reports
the second as the first. That is not hypothetical: `force_background_bash.py`'s
detector anchored on a `&&!/git/i.test(` literal, 2.1.270 deleted it, and every
patched binary read as unpatched for a month with nothing anywhere saying so.

So each detector returns UNKNOWN for "anchor gone", and this file is what makes
UNKNOWN loud: against the live binary, every detector must land on PATCHED or
STOCK. Run it after a claude update — a failure here means upstream moved the
code and the detector needs re-anchoring, whether or not the patch itself still
applies.

Run: python3 tests/test_clipatch.py
"""

import os
import sys
from pathlib import Path

HOOKS = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HOOKS))

from bgwatch_hint import _MONITOR_ANCHORS, monitor_loaded_upfront  # noqa: E402
from force_background_bash import auto_background_state  # noqa: E402
from utils._clipatch import PATCHED, STOCK, UNKNOWN, claude_binary, text_patch_state  # noqa: E402

BIN_ENV = "CLAUDE_HOOKS_CLAUDE_BIN"
failures = []


def check(name, got, want):
    if got == want:
        print(f"ok   {name}")
    else:
        print(f"FAIL {name}: got {got!r}, want {want!r}")
        failures.append(name)


def check_in(name, got, allowed):
    if got in allowed:
        print(f"ok   {name} -> {got}")
    else:
        print(f"FAIL {name}: got {got!r}, want one of {allowed}")
        failures.append(name)


def monitor_state():
    """The anchor pair of the running binary's shape (one pair per CLI shape since 2.1.271)."""
    states = [text_patch_state(stock, patched, f"monitor_undefer_test_{i}") for i, (stock, patched) in enumerate(_MONITOR_ANCHORS)]
    return next((st for st in states if st in (PATCHED, STOCK)), states[0])


# --- a missing binary is UNKNOWN, and never a confident "unpatched" ----------
os.environ[BIN_ENV] = "/nonexistent/claude"
check("missing binary: monitor detector", monitor_state(), UNKNOWN)
check("missing binary: monitor hint keeps the ToolSearch line", monitor_loaded_upfront(), False)
check("missing binary: auto-background detector", auto_background_state(), UNKNOWN)
del os.environ[BIN_ENV]

# --- the live binary must be legible to every detector -----------------------
if not claude_binary():
    print("skip claude binary not found — live-binary assertions skipped")
else:
    print(f"     live binary: {os.path.realpath(claude_binary())}")
    check_in("live binary: monitor detector anchors", monitor_state(), (PATCHED, STOCK))
    check_in("live binary: auto-background detector anchors", auto_background_state(), (PATCHED, STOCK))

# --- a pristine .orig backup, if one is around, must read STOCK --------------
# The patch scripts leave `<binary>.orig` beside the binary they patch, which is
# the only unpatched copy we can count on having locally.
orig = None
live = claude_binary()
if live:
    cand = Path(os.path.realpath(live) + ".orig")
    if cand.is_file():
        orig = cand
if orig is None:
    print("skip no .orig backup found — stock-side assertions skipped")
else:
    os.environ[BIN_ENV] = str(orig)
    print(f"     stock binary: {orig}")
    check("stock binary: monitor detector", monitor_state(), STOCK)
    check("stock binary: monitor hint keeps the ToolSearch line", monitor_loaded_upfront(), False)
    check_in("stock binary: auto-background detector", auto_background_state(), (STOCK,))
    del os.environ[BIN_ENV]

print()
if failures:
    print(f"{len(failures)} FAILED: {', '.join(failures)}")
    sys.exit(1)
print("ALL OK")
