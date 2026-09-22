#!/usr/bin/env python3
"""Regression tests for backup_conversations.select_uploads and the scanner-offender regex.

Run: VIRTUAL_ENV= uv run --project . python tests/test_backup_select.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import backup_conversations as b  # noqa: E402


def main():
    d = Path(tempfile.mkdtemp())

    def mk(name, data):
        p = d / name
        p.write_bytes(data)
        st = p.stat()
        return p, st.st_size, st.st_mtime_ns

    secret = b"x hf_ABCDEFGHIJKLMNOPQRSTUVWXYZ012345 y"
    redacted = len(b.redact_secrets(secret)[0])
    local = {
        "new.jsonl": mk("new.jsonl", b"a"),
        "same.jsonl": mk("same.jsonl", b"abc"),
        "grown.jsonl": mk("grown.jsonl", b"abcdef"),
        "secret_same.jsonl": mk("secret_same.jsonl", secret),
        "secret_grown.jsonl": mk("secret_grown.jsonl", secret + b"more"),
        "man_same.jsonl": mk("man_same.jsonl", b"zz"),
        "man_grown.jsonl": mk("man_grown.jsonl", b"zzzz"),
        "man_shrunk.jsonl": mk("man_shrunk.jsonl", b"z"),
        "remote_bigger.jsonl": mk("remote_bigger.jsonl", b"q"),
    }
    remote = {
        "same.jsonl": 3, "grown.jsonl": 3,
        "secret_same.jsonl": redacted, "secret_grown.jsonl": redacted,
        "man_same.jsonl": 2, "man_grown.jsonl": 2, "man_shrunk.jsonl": 5,
        "remote_bigger.jsonl": 9,
    }
    manifest = {
        "man_same.jsonl": [2, local["man_same.jsonl"][2]],
        "man_grown.jsonl": [2, 0],
        "man_shrunk.jsonl": [5, 0],
    }
    new, changed = b.select_uploads(local, remote, manifest)
    assert new == ["new.jsonl"], new
    assert sorted(changed) == ["grown.jsonl", "man_grown.jsonl", "secret_grown.jsonl"], changed
    # verified-current files get a manifest entry without being uploaded
    assert "same.jsonl" in manifest and "secret_same.jsonl" in manifest
    # never overwrite a remote copy with a shorter local file
    assert "remote_bigger.jsonl" not in manifest and "remote_bigger.jsonl" not in changed
    assert "man_shrunk.jsonl" not in changed

    body = "- a/b/tool-results/x.txt (ref: 1)\n- c.jsonl (ref: 2)"
    assert b._OFFENDING_FILE_RE.findall(body) == ["a/b/tool-results/x.txt", "c.jsonl"]
    print("OK")


if __name__ == "__main__":
    main()
