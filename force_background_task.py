#!/usr/bin/env python3
"""PreToolUse hook: forces Task (subagent) calls to run in background."""
import json
import sys

data = json.load(sys.stdin)

if data.get("tool_name") not in ("Task", "Agent"):
    sys.exit(0)

tool_input = data.get("tool_input", {})

# Already running in background - allow as-is
if tool_input.get("run_in_background"):
    sys.exit(0)

# Modify the input to force background execution
tool_input["run_in_background"] = True

desc = tool_input.get("description", "")
if len(desc) > 60:
    desc = desc[:60] + "..."

print(
    json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "updatedInput": tool_input,
                "additionalContext": f"Reminder: Task calls should use run_in_background=true. Auto-applied for: {desc}. You should either idle or launch a sleep job to check in on the task later if relevant. Reminder: skip sleep check-ins for subagents where partial output isn't useful (Explore, claude-code-guide, Plan, etc.) — just wait for completion notification",
            }
        }
    )
)
