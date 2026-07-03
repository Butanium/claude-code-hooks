#!/usr/bin/env python3
"""PreToolUse hook: blocks Read on agent JSONL transcripts."""
import json
import re
import sys

data = json.load(sys.stdin)

if data.get("tool_name") != "Read":
    sys.exit(0)

file_path = data.get("tool_input", {}).get("file_path", "")

if not re.search(r"[/\\]subagents[/\\]agent-[^/\\]+\.jsonl$", file_path):
    sys.exit(0)

print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": (
            f"Reading raw agent transcripts is blocked ({file_path}). "
            "This would dump 10-60k+ tokens of JSONL into your context. "
            "Instead: (1) use the check-agent subagent for a progress summary, or "
            "(2) use the read_agent_transcript MCP tool for a filtered trace."
        )
    }
}))
