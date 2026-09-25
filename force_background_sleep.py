#!/usr/bin/env python3
"""PreToolUse hook: auto-background sleep commands for main agent / teammates.

Sleeps used as watchdogs/pings should run in background so a real task
finishing earlier doesn't strand the agent on the timer. Subagents are
excluded — ending their turn returns them to the parent, so they can't wait for
the completion notification (`utils/_agent_kind.is_subagent`).

Patterns matched (anywhere a sleep is the wait primitive):
- command starts with ``sleep `` (after stripping leading whitespace)
- command contains ``do sleep `` (loop watchdog: ``while ...; do sleep N; done``)

Heredoc bodies are ignored: a script or test fixture being written that
mentions ``do sleep`` is not a wait (ENGINEERING_LOGS.md, 2026-09-22).
"""
import json
import re
import sys

from no_tail_head_pipes import HEREDOC
from utils._agent_kind import is_subagent


def is_watchdog(cmd: str) -> bool:
    cmd = HEREDOC.sub(lambda m: m.group(0).split("\n", 1)[0], cmd)
    return cmd.lstrip().startswith("sleep ") or re.search(r"\bdo\s+sleep\s", cmd) is not None


def main():
    data = json.load(sys.stdin)
    if data.get("tool_name") != "Bash":
        return
    tool_input = data.get("tool_input", {})
    cmd = tool_input.get("command", "")
    if is_subagent(data):
        return
    if tool_input.get("run_in_background") or not is_watchdog(cmd):
        return

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


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Non-blocking error: surfaced to the user, the command still runs.
        import traceback

        traceback.print_exc()
        sys.exit(1)
