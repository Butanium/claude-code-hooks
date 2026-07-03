#!/usr/bin/env python3
"""PreToolUse hook: blocks shutdown_request via SendMessage unless reason includes acknowledgment.

Forces the team lead to explicitly confirm that Clément won't need to review
or discuss the teammate's work before shutting them down.

Exception: if the last user message contains "shutdown", allow without the tag
(Clément explicitly requested it).
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
import sys as _sys; from pathlib import Path as _P; _sys.path.insert(0, str(_P(__file__).resolve().parent.parent))
from utils._last_message import get_last_user_text, wait_for_transcript_flush

REQUIRED_TAG = "[I ASSUME CLÉMENT WON'T WANT TO DISCUSS/AUDIT YOU]"

data = json.load(sys.stdin)

if data.get("tool_name") != "SendMessage":
    sys.exit(0)

tool_input = data.get("tool_input", {})
message = tool_input.get("message")

# Only intercept structured shutdown_request messages
if not isinstance(message, dict) or message.get("type") != "shutdown_request":
    sys.exit(0)

reason = message.get("reason", "")
if REQUIRED_TAG.lower() in reason.lower():
    sys.exit(0)

# If Clément's last message contains "shutdown", he explicitly requested it — allow
transcript_path = data.get("transcript_path", "")
if transcript_path and os.path.exists(transcript_path):
    wait_for_transcript_flush(transcript_path)
    last_user = get_last_user_text(transcript_path)
    if "shutdown" in last_user.lower():
        sys.exit(0)

print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": (
            f"Shutdown requests must include {REQUIRED_TAG} in the reason "
            "to confirm you've considered whether Clément might want to review "
            "or discuss this teammate's work. Ask yourself: did the teammate produce "
            "results Clément might want to audit, discuss, or ask follow-up questions "
            "about? If so, keep them around. If you're sure, add the tag to the reason."
        ),
    }
}))
