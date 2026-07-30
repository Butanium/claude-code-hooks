#!/usr/bin/env python3
"""PreToolUse hook on SendMessage: blocks pings when the recipient has already replied.

Kills the defensive-ping antipattern. Before any agent (lead or teammate)
sends a message, this checks whether the would-be recipient already has a
pending reply in the agent's own inbox that hasn't yet been delivered (i.e.
doesn't yet appear as a `<teammate-message>` block in their transcript).

If so, the hook denies the SendMessage with a short preview of the pending
entries plus an instruction to end the turn so the harness can flush them.
The full content is delivered through the normal harness path on end_turn,
keeping context clean.

Identifies the sending agent via:
- `agent_id` from hook input (formatted "<member>@<team>" for team members);
  if absent, falls back to scanning team configs for a matching leadSessionId.
- `transcript_path` from hook input (no need to glob projects/).

Bypass: include `[ACK-PENDING]` in `message` or `summary` (for
`shutdown_request`, in `reason`) to override.

Dict-form protocol *responses* (`shutdown_response`,
`plan_approval_response`) pass through unrestricted — they answer the
recipient's own request, so blocking them can only deadlock the exchange.
`shutdown_request` is guarded like a text message: it's the one send where
acting blind to an unseen reply matters most (2026-07-30: a lead asked its
reviewer "anything left before shutdown?", the reviewer answered with a real
finding, and the lead — seeing only `teammate_status` "idle" — sent the
shutdown_request while that answer sat undelivered in its inbox).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

HELPER_DIR = Path.home() / ".claude" / "mcp" / "team-inbox"
sys.path.insert(0, str(HELPER_DIR))

from inbox_state import (  # noqa: E402
    find_team_by_lead_session,
    inbox_path as inbox_path_for,
    pending_from_paths,
)

PREVIEW_CHARS = 100


def resolve_context(data: dict) -> tuple[str, str] | None:
    """Return (team_name, member_name) for the calling session, or None."""
    agent_id = data.get("agent_id", "") or ""
    if "@" in agent_id:
        member, team = agent_id.split("@", 1)
        if team and member:
            return team, member
    session_id = data.get("session_id", "")
    if session_id:
        team, _ = find_team_by_lead_session(session_id)
        if team:
            return team, "team-lead"
    return None


def preview_text(m: dict) -> str:
    """Short one-line preview for the deny reason."""
    summary = (m.get("summary") or "").strip()
    text = m.get("text", "") or ""
    n = len(text)
    if summary:
        return f"{summary}  ({n} chars)"
    snippet = text.replace("\n", " ").strip()
    if len(snippet) > PREVIEW_CHARS:
        snippet = snippet[:PREVIEW_CHARS].rstrip() + "…"
    return f"{snippet}  ({n} chars)"


def main() -> None:
    data = json.load(sys.stdin)

    if data.get("tool_name") != "SendMessage":
        return

    tool_input = data.get("tool_input", {})
    to = tool_input.get("to", "")
    if not to:
        return

    # Dict-form responses pass (see module docstring); shutdown_request is
    # guarded like text, with the bypass tag carried in its `reason` field.
    msg = tool_input.get("message")
    if isinstance(msg, dict):
        if msg.get("type") != "shutdown_request":
            return
        bypass_haystack = str(msg.get("reason", ""))
    elif isinstance(msg, str):
        bypass_haystack = msg
    else:
        return

    summary = tool_input.get("summary", "") or ""
    if "[ACK-PENDING]" in bypass_haystack or "[ACK-PENDING]" in summary:
        return

    ctx = resolve_context(data)
    if ctx is None:
        return
    team_name, member_name = ctx

    inbox_p = inbox_path_for(team_name, member_name)
    if not inbox_p.exists():
        return
    try:
        inbox = json.loads(inbox_p.read_text())
    except json.JSONDecodeError:
        return

    transcript_p = data.get("transcript_path")
    transcript_path = Path(transcript_p) if transcript_p else None

    pending = pending_from_paths(
        inbox=inbox,
        transcript_path=transcript_path,
        sender_filter=to,
        drop_protocol=True,
    )
    if not pending:
        return

    lines = [
        f"You have {len(pending)} pending message(s) from '{to}' that you "
        f"haven't seen yet. End your turn (stop emitting tool calls) so the "
        f"harness flushes them — you'll see the full content on your next "
        f"turn. To send anyway after acknowledging the reply, include "
        f"[ACK-PENDING] in your message or summary (for a shutdown_request: "
        f"in its reason field).",
        "",
        "Preview:",
    ]
    for i, m in enumerate(pending, 1):
        ts = m.get("timestamp", "?")
        lines.append(f"[{i}] @ {ts} — {preview_text(m)}")

    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": "\n".join(lines),
                }
            }
        )
    )


if __name__ == "__main__":
    main()
