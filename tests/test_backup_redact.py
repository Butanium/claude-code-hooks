#!/usr/bin/env python3
"""Regression tests for backup_conversations.redact_secrets on transcript-shaped bytes.

Run: VIRTUAL_ENV= uv run --project . python tests/test_backup_redact.py
Every secret below is synthetic.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

secrets_file = Path(tempfile.mkdtemp()) / "secrets.sh"
secrets_file.write_text("export ODDLY_NAMED_THING=Zq8vLm3TnB5wXc1R\nPLAIN_CRED=ignored\n")
os.environ["CLAUDE_SECRETS_FILE"] = str(secrets_file)
import backup_conversations as b  # noqa: E402

FAKE = "Qm7Xk2Lp9Rt4Vw8Yz3Nb"  # 20 chars, token-shaped


def line(text: str) -> bytes:
    """One JSONL record carrying `text` as a tool result, JSON-escaped like the CLI writes it."""
    return json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "content": text}]}}).encode()


def check(label: str, text: str, gone: str, kept: tuple[str, ...] = ()):
    out, counts = b.redact_secrets(line(text))
    assert gone.encode() not in out, f"{label}: secret survived"
    for k in kept:
        assert k.encode() in out, f"{label}: {k!r} was redacted but should not be"
    assert json.loads(out), f"{label}: output is no longer valid JSON"
    print(f"ok   {label}  {counts}")


def check_kept(label: str, text: str):
    out, _ = b.redact_secrets(line(text))
    assert out == line(text), f"{label}: changed but should not have"
    print(f"ok   {label}  (untouched)")


check("modal secret", f"token as-{FAKE}xx ready", FAKE)
check("modal id", f"id=ak-{FAKE}yy", FAKE)
check("runpod key", f"key rpa_{FAKE}ZZ9 set", FAKE)
check("export NAME_SECRET=", f"export MODEL_TOKEN_SECRET={FAKE}\nexport OTHER=1", FAKE, ("OTHER=1",))
check("env dump NAME_TOKEN=", f"PATH=/usr/bin\nWANDB_TOKEN={FAKE}\nHOME=/h", FAKE, ("PATH=/usr/bin", "HOME=/h"))
check("quoted NAME_API_KEY=", f"OPENROUTER_API_KEY=\"{FAKE}\" python run.py", FAKE, ("python run.py",))
check("json \"NAME_KEY\": \"v\"", json.dumps({"SERVICE_KEY": FAKE, "region": "eu"}), FAKE, ("region",))
check("secrets-file name", f"ODDLY_NAMED_THING={FAKE}", FAKE)
check_kept("reference $VAR", "export HF_TOKEN=$HF_TOKEN_PROD")
check_kept("reference OTHER_VAR", "SOME_TOKEN=HF_TOKEN_PROD")
check_kept("short value", "RETRY_KEY=abc")
check_kept("prose mentions name", "set MODEL_TOKEN_SECRET before running")
check_kept("not secret-named", f"RUN_NAME={FAKE}")

# size stays sane: redaction only ever shortens a line
raw = line(f"X_SECRET={FAKE}")
assert len(b.redact_secrets(raw)[0]) < len(raw)
print("ALL OK")
