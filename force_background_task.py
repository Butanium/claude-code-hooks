#!/usr/bin/env python3
"""PreToolUse hook: forces Task (subagent) calls to run in background.

Since ~2.1.271 the Agent tool has no `run_in_background` parameter and always
runs agents in the background, so on current CLIs the `updatedInput` below is
inert. It is kept, silently, for older CLIs where the parameter still exists and
defaults to foreground. No `additionalContext`: the reminder this used to inject
fired on every Agent call once the parameter disappeared, and its "launch a sleep
job to check in" advice contradicted no_poll_background.py.
"""
import json
import os
import sys
import traceback


def main():
    data = json.load(sys.stdin)

    if data.get("tool_name") not in ("Task", "Agent"):
        return

    tool_input = data.get("tool_input", {})

    # agent_worktree_teammate.py rewrites these (and sets run_in_background itself);
    # a second updatedInput for the same call would race it.
    sys.path.insert(0, os.path.dirname(__file__))
    from agent_worktree_teammate import is_candidate

    if is_candidate(tool_input) or tool_input.get("run_in_background"):
        return

    tool_input["run_in_background"] = True
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": tool_input}}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Non-blocking error: surfaced to the user, the Agent call still runs.
        traceback.print_exc()
        sys.exit(1)
