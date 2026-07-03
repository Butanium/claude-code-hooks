#!/usr/bin/env python3
"""PreToolUse hook: auto-background sleep commands for main agent / teammates.

Sleeps used as watchdogs/pings should run in background so a real task
finishing earlier doesn't strand the agent on the timer. Subagents are
excluded — they don't receive background-completion notifications.

Patterns matched (anywhere a sleep is the wait primitive):
- command starts with ``sleep `` (after stripping leading whitespace)
- command contains ``do sleep `` (loop watchdog: ``while ...; do sleep N; done``)
"""
import json
import re
import sys

data = json.load(sys.stdin)

if data.get("tool_name") != "Bash":
    sys.exit(0)

tool_input = data.get("tool_input", {})
cmd = tool_input.get("command", "")
agent_id = data.get("agent_id", "")
is_subagent = bool(agent_id) and "@" not in agent_id

if is_subagent:
    sys.exit(0)

if tool_input.get("run_in_background"):
    sys.exit(0)

stripped = cmd.lstrip()
matches = (
    stripped.startswith("sleep ")
    or re.search(r"\bdo\s+sleep\s", cmd) is not None
)
if not matches:
    sys.exit(0)

tool_input["run_in_background"] = True
cmd_preview = cmd[:10] + "..." if len(cmd) > 10 else cmd
message = (
    f"Auto-backgrounded as a sleep/watchdog: {cmd_preview}\n"
    "Idle until the completion notification — don't poll. Watchdogs run in "
    "background so that if the real task you're waiting on finishes first, "
    "you can act on it immediately instead of sitting out the rest of the timer."
)
print(
    json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "updatedInput": tool_input,
                "additionalContext": message,
            }
        }
    )
)
