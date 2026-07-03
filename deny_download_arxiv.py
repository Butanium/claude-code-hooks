#!/usr/bin/env python3
"""PreToolUse hook: blocks download_arxiv MCP tool in favor of /read-arxiv skill."""
import json
import sys

data = json.load(sys.stdin)

if data.get("tool_name") != "mcp__paper-search__download_arxiv":
    sys.exit(0)

print(
    json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": (
                    "Don't use download_arxiv — load the /read-arxiv skill instead. "
                    "It handles downloading, unpacking, and reading arxiv papers properly."
                    "Please, do load the skill, as it will streamline the process for both of us."
                ),
            }
        }
    )
)
