#!/usr/bin/env python3
"""PreToolUse hook for Bash sync timeout policy.

- Main agent / teammates, requested sync timeout > 60s:
  - Command auto-backgroundable at timeout: clamp the timeout to 30s. The
    CLI moves such commands to background when the sync timeout expires (no
    kill), so the clamp only caps how long the conversation blocks. Goal: a
    long sync wait burns turns for no reason — if you expect >60s, you should
    have used run_in_background=true upfront.
  - Command NOT auto-backgroundable: deny with advice. For these the sync
    timeout is a hard SIGTERM kill (exit 143), so clamping would kill work
    the model sized its timeout to protect (this bit us: a 240s request
    clamped to 30s killed a pipeline mid-run with partial output).
- Subagents: block `run_in_background=true` unless command contains
  BACKGROUND_NEEDED escape hatch (e.g. starting a server). High sync timeouts
  remain allowed because subagents can't usefully background — they don't
  receive completion notifications.

Auto-backgroundability (CLI v2.1.216; undocumented, details in
https://github.com/anthropics/claude-code/issues/79879): the CLI's static
shell analyzer must fully decompose the command, no git subcommand, first
word not sleep. KILL_CLASS_RE approximates the analyzer's rejections we
verified empirically ($VAR/backtick redirect targets, heredocs, process
substitution). A false positive here just means deny-with-advice instead of
clamp, which is safe; a false negative means the old behavior (clamp, kill
at 30s), no worse than before this check existed.

Teammates are distinguished from subagents by agent_id format: teammate IDs
look like ``name@team_name``, subagent IDs are bare hex. The main agent has
no agent_id at all. (In tmux/pane teammate mode agent_id is also absent —
those fall through to main-agent rules, which is the intent.)
"""
import json
import re
import sys

KILL_CLASS_RE = re.compile(
    r"<<"  # heredoc / herestring (heredoc + file redirect is a verified kill; be conservative)
    r"|[<>]\("  # process substitution
    r"|[<>]\|?\s*[\"']?[$`]"  # $VAR or `...` as a redirect target (verified kill)
)
GIT_RE = re.compile(r"(?:^|[;&|(]|\$\(|`)\s*(?:command\s+|builtin\s+)?git\b")
SLEEP_RE = re.compile(r"^\s*sleep\b")


def is_kill_class(command: str) -> bool:
    """True if the CLI would SIGTERM-kill this command at sync timeout
    instead of moving it to background (approximation, see module docstring)."""
    return bool(
        KILL_CLASS_RE.search(command)
        or GIT_RE.search(command)
        or SLEEP_RE.match(command)
    )

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

# --- Main agent / teammate: sync timeout policy for > 60s requests ---
timeout = tool_input.get("timeout", 10000)
if timeout <= 60000:
    sys.exit(0)

original_timeout_s = int(timeout / 1000)

if is_kill_class(cmd):
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        f"BLOCKED: you requested a {original_timeout_s}s sync "
                        "timeout for a command Claude Code cannot move to "
                        "background at timeout (it contains a heredoc, a "
                        "$VAR/backtick redirect target, git, or leading sleep "
                        "— for these, sync timeout is a hard SIGTERM kill, "
                        "see https://github.com/anthropics/claude-code/issues/79879). "
                        "A long sync wait here risks losing the work at the "
                        "timeout boundary. Re-run with run_in_background=true "
                        "and monitor it (Monitor / task notification), or "
                        "restructure the command (literal redirect paths, no "
                        "heredoc combined with a redirect) so it can be "
                        "auto-backgrounded."
                    ),
                }
            }
        )
    )
    sys.exit(0)

tool_input["timeout"] = 30000
message = (
    f"Sync timeout policy: you requested a {original_timeout_s}s sync timeout "
    "(>60s); clamped to 30s. This command passed the auto-background check, so "
    "when the 30s sync timeout expires the Bash tool moves it to background "
    "(it doesn't kill it) — the clamp only caps how long the conversation "
    "blocks. If you expect this to take >60s, prefer run_in_background=true "
    "upfront to skip the sync wait entirely, which can allow you to work on "
    "other stuff while it's running and have stronger monitors: if this task "
    "requires it, don't forget to monitor it properly with Monitor or /loop."
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
