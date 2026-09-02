"""UTF-8 stdio for hooks.

On Windows, Python defaults stdout/stderr to the ANSI code page (cp1252 here),
so printing a single emoji raises UnicodeEncodeError, the hook exits nonzero,
Claude Code renders "<Event> hook error", and everything the hook still had to
print is lost. `detect_env.py` lost the whole session-start identity block that
way: the `⚠️` on an unset-env warning killed it two statements early.

Text file IO has the mirror-image failure — transcripts are UTF-8 and the
default decode is cp1252 — but `open()`'s default can't be changed from inside
the process, so hooks pass `encoding="utf-8"` explicitly at those call sites.
"""

from __future__ import annotations

import sys


def utf8_stdio() -> None:
    """Switch stdout/stderr to UTF-8. Call before printing anything non-ASCII."""
    for stream in (sys.stdout, sys.stderr):
        # A hook can be spawned with stdio swapped for an object with no
        # reconfigure() (a plain pipe wrapper, a test double); it has whatever
        # encoding its owner chose and is not ours to change.
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
