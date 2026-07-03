#!/usr/bin/env python3
"""PreToolUse hook for Bash sync timeout policy.

- Main agent / teammates: if requested sync timeout > 60s, clamp it to 30s.
  Claude Code's Bash tool auto-moves the command to background once timeout
  expires (it doesn't kill it), so this just caps how long the conversation
  blocks before the command becomes a background task. Goal: a long sync wait
  burns turns for no reason — if you expect >60s, you should have used
  run_in_background=true upfront to skip the sync portion entirely.
- Subagents: block `run_in_background=true` unless command contains
  BACKGROUND_NEEDED escape hatch (e.g. starting a server). High sync timeouts
  remain allowed because subagents can't usefully background — they don't
  receive completion notifications.

Teammates are distinguished from subagents by agent_id format: teammate IDs
look like ``name@team_name``, subagent IDs are bare hex. The main agent has
no agent_id at all. (In tmux/pane teammate mode agent_id is also absent —
those fall through to main-agent rules, which is the intent.)
"""
import json
import sys

data = json.load(sys.stdin)

if data.get("tool_name") != "Bash":
    sys.exit(0)

tool_input = data.get("tool_input", {})
cmd = tool_input.get("command", "")
agent_id = data.get("agent_id", "")
is_subagent = bool(agent_id) and "@" not in agent_id

# --- Subagent: block run_in_background unless escape hatch ---
if is_subagent and tool_input.get("run_in_background"):
    if "BACKGROUND_NEEDED" in cmd:
        sys.exit(0)
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        "BLOCKED: Subagents cannot use run_in_background=true. "
                        "Unlike the main agent, subagents do not receive background "
                        "task completion notifications — this leads to doom loops "
                        "where you poll repeatedly wasting turns. Instead, run the "
                        "command synchronously with a high timeout (e.g. "
                        "timeout=600000 for 10min). If you genuinely need background "
                        "execution (e.g. starting a server), include "
                        "BACKGROUND_NEEDED in your command: "
                        "echo BACKGROUND_NEEDED && your_actual_command"
                    ),
                }
            }
        )
    )
    sys.exit(0)

# --- Subagent without background: high sync timeouts allowed (only mode they have) ---
if is_subagent:
    sys.exit(0)

# --- Main agent / teammate: already background, leave alone ---
if tool_input.get("run_in_background"):
    sys.exit(0)

# --- Main agent / teammate: clamp sync timeout if > 60s ---
timeout = tool_input.get("timeout", 10000)
if timeout <= 60000:
    sys.exit(0)

original_timeout_s = int(timeout / 1000)
tool_input["timeout"] = 30000
message = (
    f"Sync timeout policy: you requested a {original_timeout_s}s sync timeout "
    "(>60s); clamped to 30s. The Bash tool auto-moves the command to background "
    "once timeout expires (it doesn't kill it), so a long sync timeout just "
    "makes the conversation block longer for no benefit. If you expect this "
    "to take >60s, prefer run_in_background=true upfront to skip the sync "
    "wait entirely, which can allow you to work on other stuff while it's running and have stronger monitors: if this task requires it, don't forget to monitor it properly with Monitor or /loop."
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
