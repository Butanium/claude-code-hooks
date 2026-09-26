#!/usr/bin/env python3
"""Regression tests for backup_conversations.redact_secrets on transcript-shaped bytes.

Run: VIRTUAL_ENV= uv run --project . python tests/test_backup_redact.py
Cases live in tests/redaction_cases.json, shared byte-for-byte with whowas (its
index-time redaction applies the same rules); if whowas's copy is on disk, it must match.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
CASES_FILE = HERE / "redaction_cases.json"
cases = json.loads(CASES_FILE.read_text())

secrets_file = Path(tempfile.mkdtemp()) / "secrets.sh"
secrets_file.write_text(cases["secrets_file"])
os.environ["CLAUDE_SECRETS_FILE"] = str(secrets_file)
import backup_conversations as b  # noqa: E402

FAKE = cases["fake"]


def line(text: str) -> bytes:
    """One JSONL record carrying `text` as a tool result, JSON-escaped like the CLI writes it."""
    return json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "content": text}]}}).encode()


for c in cases["redact"]:
    text = c["text"].replace("{FAKE}", FAKE)
    out, counts = b.redact_secrets(line(text))
    assert FAKE.encode() not in out, f"{c['label']}: secret survived"
    for k in c["kept"]:
        assert k.encode() in out, f"{c['label']}: {k!r} was redacted but should not be"
    assert json.loads(out), f"{c['label']}: output is no longer valid JSON"
    print(f"ok   {c['label']}  {counts}")
for c in cases["keep"]:
    raw = line(c["text"].replace("{FAKE}", FAKE))
    assert b.redact_secrets(raw)[0] == raw, f"{c['label']}: changed but should not have"
    print(f"ok   {c['label']}  (untouched)")

twin = Path.home() / ".claude" / "tools" / "whowas" / "redaction_cases.json"
if twin.exists():
    assert twin.read_bytes() == CASES_FILE.read_bytes(), f"{twin} drifted from {CASES_FILE}: copy one over the other"
    print("ok   whowas's copy of the fixture is identical")
print("ALL OK")
