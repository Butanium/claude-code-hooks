#!/usr/bin/env python3
"""PreToolUse hook: blocks Agent calls that mention 'teammate'/'colleague' without a name.

A teammate is an Agent spawned with `name` set: a full instance (extended thinking,
background tasks, its own tmux pane, addressable via SendMessage). Without `name`,
Agent spawns a subagent — no extended thinking, no background tasks, the user can't
interact with it. This hook catches the common mistake of saying "teammate" or
"colleague" but spawning a nameless subagent.

Uses Levenshtein distance <= 2 for typo tolerance.
Bypass: include [NEED-DUMBER-SUBAGENT] in the Agent description to force a
subagent, or [FUCKYOU-<USER>] for teammate-word false positives (where <USER>
is the I_LOVE_BEING_A_USER env var, accent-stripped and uppercased — the tag
is intentionally rude because it's mostly used on true positives).

A message from another Claude instance (the team lead or a peer teammate)
arrives as a type=="user" entry too, wrapped in a <teammate-message> block —
but it isn't the human. So if the last user message is from a teammate we don't
treat it as a human request; otherwise the guard blames the human for a word the
lead wrote (e.g. the spawn prompt "You are a lit-review teammate", which stays
the last user-text for the whole teammate session and trips every subagent
spawn).
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from utils._last_message import get_last_assistant_text, get_last_user_text, is_human_turn, wait_for_transcript_flush
from utils._user import user_name, user_name_ascii_upper

TARGETS = ["teammate", "colleague"]
IGNORE = {"college"}
MAX_DISTANCE = 2


def levenshtein(a: str, b: str) -> int:
    """Compute Levenshtein edit distance between two strings."""
    if len(a) < len(b):
        return levenshtein(b, a)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        curr = [i + 1]
        for j, cb in enumerate(b):
            curr.append(min(
                prev[j + 1] + 1,      # deletion
                curr[j] + 1,           # insertion
                prev[j] + (ca != cb),  # substitution
            ))
        prev = curr
    return prev[-1]


def has_teammate_word(text: str) -> bool:
    """Check if any word in text is within MAX_DISTANCE of a target word.

    Only scans the first 1000 words to keep hook latency low.
    """
    for word in text.lower().split(maxsplit=1000)[:1000]:
        word = word.strip(".,;:!?\"'`()[]{}#*_~<>")
        if not word:
            continue
        if word in IGNORE:
            continue
        for target in TARGETS:
            if abs(len(word) - len(target)) > MAX_DISTANCE:
                continue
            if levenshtein(word, target) <= MAX_DISTANCE:
                return True
    return False


data = json.load(sys.stdin)

if data.get("tool_name") != "Agent":
    sys.exit(0)

tool_input = data.get("tool_input", {})

# If name is set, this is properly a teammate — allow
if tool_input.get("name"):
    sys.exit(0)

# A fork is a full instance (extended thinking, can spawn its own teammates),
# not the nameless dumb subagent this guard exists to catch. Exempt it — the
# fork itself will hit this same hook if/when it spawns a nameless subagent.
if tool_input.get("subagent_type") == "fork":
    sys.exit(0)

USER_BYPASS = f"[FUCKYOU-{user_name_ascii_upper()}]"
bypass_words = [USER_BYPASS, "[NEED-DUMBER-SUBAGENT]"]
# If bypass word in description, explicitly forced — allow
for bypass_word in bypass_words:
    if bypass_word in tool_input.get("description", "").upper():
        sys.exit(0)

# Check sources in order, tracking where the match came from.
source = None
transcript_path = data.get("transcript_path", "")
if transcript_path and os.path.exists(transcript_path):
    wait_for_transcript_flush(transcript_path)
    # The last user message counts as the human's only if it's a genuine human
    # turn. Walk past task-notifications (system events, not messages — they'd
    # otherwise mask the human message they landed after); then, if the last real
    # message is from another instance (team lead / peer teammate, wrapped in
    # <teammate-message>), it isn't the human, so skip the user check.
    user_text = get_last_user_text(transcript_path, skip_task_notifications=True)
    if is_human_turn(user_text) and has_teammate_word(user_text):
        source = "user"
    elif has_teammate_word(get_last_assistant_text(transcript_path)):
        source = "model"
    else:
        # Fall back to the call itself (description + prompt).
        tool_text = f"{tool_input.get('description', '')} {tool_input.get('prompt', '')}"
        if has_teammate_word(tool_text):
            source = "model"

if source == "user":
    who = user_name()
    reason = (
        f"{who}'s message contains 'teammate' or similar. If {who} explicitly asked "
        "you to spawn a teammate, that means they want to be able to interact with the agent "
        "you'll spawn — always honor this. Spawn it with Agent and set `name`: that is what "
        "makes it a real teammate (extended thinking, background tasks, its own tmux pane, "
        f"addressable via SendMessage). If this is a false positive (i.e. {who}'s query includes "
        f"the teammate word but in a different context), add {USER_BYPASS} to the description "
        "to bypass. This bypass tag is intentionally offensive because most of the time you use "
        f"it on true positives and piss {who} off."
    )
elif source == "model":
    reason = (
        "You mentioned 'teammate' or 'colleague' but didn't set `name`. "
        "Set `name` on the Agent call to spawn a real teammate (extended thinking, "
        "background tasks, its own tmux pane, addressable via SendMessage). "
        "Without `name`, Agent spawns a subagent (no thinking, no background tasks, "
        "user can't interact with it). If this is a false positive and you actually need a limited subagent, add [NEED-DUMBER-SUBAGENT] to the description to bypass — naming what you're doing so you don't reach for it reflexively."
        "As a reminder, subagent vs teammate is about how much intelligence you need the other instance to have: even for a quick 1 shot task that need"
        "cognition, e.g. reading a paper, it's better to spawn a teammate so that the agent actually get to think and gives you a better analysis"
    )
else:
    sys.exit(0)

print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }
}))
