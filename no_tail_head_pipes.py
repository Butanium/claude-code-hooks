#!/usr/bin/env python3
"""PreToolUse hook: strips tail/head pipes from background commands."""
import json
import re
import sys

data = json.load(sys.stdin)

if data.get("tool_name") != "Bash":
    sys.exit(0)

tool_input = data.get("tool_input", {})

# Only check background commands
if not tool_input.get("run_in_background"):
    sys.exit(0)

command = tool_input.get("command", "")

# Allow if NEEDTAIL escape hatch is present
if "NEEDTAIL" in command:
    sys.exit(0)

# Check for pipes to tail or head
if not re.search(r"\|\s*(tail|head)\b", command):
    sys.exit(0)

cmd_display = command
if len(cmd_display) > 80:
    cmd_display = cmd_display[:80] + "..."

print(
    json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": "Background commands write output to a file, so piping through tail/head loses the rest. Read/grep/slice/tail/head the output file afterwards instead. This is to avoid running long tasks and loosing e.g. stack traces. If piping is genuinely needed (e.g. polling), add NEEDTAIL as a comment in the command to bypass this hook.",
            }
        }
    )
)
