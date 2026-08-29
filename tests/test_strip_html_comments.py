#!/usr/bin/env python3
"""Regression tests for detect_env.strip_html_comments.

The bug being pinned: a comment occupying a whole template line used to leave its
newline behind, so commenting out one bullet of a markdown list punched a blank
line into the generated CLAUDE.md and split the list in two.

Run: python3 tests/test_strip_html_comments.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from detect_env import strip_html_comments  # noqa: E402


def check(label, got, want):
    ok = got == want
    print(f"{'ok  ' if ok else 'FAIL'} {label}" + (f" — got {got!r}, want {want!r}" if not ok else ""))
    if not ok:
        sys.exit(1)


check(
    "whole-line comment takes its line with it",
    strip_html_comments("- a\n<!-- - b -->\n- c\n"),
    "- a\n- c\n",
)

check(
    "consecutive whole-line comments leave nothing",
    strip_html_comments("- a\n<!-- - b -->\n<!-- - c -->\n- d\n"),
    "- a\n- d\n",
)

check(
    "indented whole-line comment takes its line too",
    strip_html_comments("- a\n    <!-- - b -->  \n- c\n"),
    "- a\n- c\n",
)

check(
    "multi-line comment block takes all its lines",
    strip_html_comments("intro\n<!--\nheld\nfor history\n-->\nafter\n"),
    "intro\nafter\n",
)

check(
    "unterminated final line (no trailing newline)",
    strip_html_comments("- a\n<!-- - b -->"),
    "- a\n",
)

check(
    "inline comment is cut out in place, line survives",
    strip_html_comments("- a <!-- aside --> b\n"),
    "- a  b\n",
)

check(
    "comment closing mid-line keeps the trailing text",
    strip_html_comments("start\n<!-- b\nc --> tail\n"),
    "start\n tail\n",
)

check(
    "blank lines around a comment are preserved",
    strip_html_comments("para\n\n<!-- note -->\n\npara2\n"),
    "para\n\n\npara2\n",
)

check("text without comments is untouched", strip_html_comments("- a\n- b\n"), "- a\n- b\n")

print("ALL OK")
