#!/usr/bin/env python3
"""Stop/SubagentStop hook: block the stop if the last entry is a synthetic API-error message.

Motivating case: the CLI exhausts its retry budget on a 503 (or similar) and ends
the turn with a synthetic assistant message. The agent didn't choose to stop —
they got dropped. This hook nudges them to pick back up.

Bypass: if the most recent non-synthetic assistant message contains [IAMOK],
the stop goes through. That handles the false-alarm case — the agent was
already done and the API error happened on what would've been a no-op turn,
so the nudge isn't needed.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from utils._last_message import get_last_jsonl_entry

BYPASS_TAG = "[IAMOK]"

NUDGE = (
    "Stop hook here. Your last turn ended on a synthetic API-error message, "
    "not a real stop — the CLI exhausted its retry budget upstream. Pick up "
    "where you were. If this is a false alarm and you actually did want to "
    f"stop (e.g., the error landed on a turn you'd already finished), reply "
    f"with `{BYPASS_TAG}` and the next stop will pass through."
)


def last_is_api_error(entry: dict | None) -> bool:
    if not entry:
        return False
    if entry.get("isApiErrorMessage"):
        return True
    if entry.get("apiErrorStatus"):
        return True
    return False


def last_non_synthetic_assistant_text(transcript_path: str) -> str:
    """Most recent assistant text whose model is not '<synthetic>'."""
    last = ""
    with open(transcript_path) as f:
        for line in f:
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if obj.get("type") != "assistant":
                continue
            msg = obj.get("message", {})
            if msg.get("model") == "<synthetic>":
                continue
            content = msg.get("content", "")
            if isinstance(content, list):
                text = " ".join(
                    c.get("text", "") for c in content if c.get("type") == "text"
                )
            else:
                text = str(content)
            if text.strip():
                last = text.strip()
    return last


def main() -> None:
    data = json.load(sys.stdin)
    if data.get("hook_event_name") not in ("Stop", "SubagentStop"):
        return

    transcript_path = data.get("transcript_path", "")
    if not transcript_path or not os.path.exists(transcript_path):
        return

    if not last_is_api_error(get_last_jsonl_entry(transcript_path)):
        return

    if BYPASS_TAG in last_non_synthetic_assistant_text(transcript_path):
        return

    print(json.dumps({"decision": "block", "reason": NUDGE}))


if __name__ == "__main__":
    main()
