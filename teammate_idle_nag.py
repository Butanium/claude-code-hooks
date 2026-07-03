#!/usr/bin/env python3
"""TeammateIdle hook: nag teammates who go idle without replying to lead.

Skips firing if any of the following are true:
- Teammate has in-flight background work (per inflight_tracker.py state).
- Teammate already replied to the most recent plain-text lead message
  via a SendMessage tool call (to="team-lead", non-protocol body) that
  actually went through — a SendMessage whose tool_result is is_error
  (e.g. rejected for a missing `summary`) never reached the lead, so it
  does NOT count and the nag still fires.
- Teammate deliberately chose silence: assistant final text since the
  last lead message contains [silent-close] (for when the exchange has
  closed on both sides and a reply would just be noise).
- No prior lead message exists in the transcript.

If the human user has DM'd the teammate since the last
lead message, the hook stands down entirely — they're actively in
conversation and the lead's (now-superseded) message can wait. A
fresh lead message re-anchors and nagging resumes normally, so the
suppression is per-message, not permanent.

Exception: a human DM containing the `[steer]` tag is treated as a
quick course-correction, not a conversational takeover — it does NOT
suppress the nag, so the teammate is still reminded to reply to the
lead before idling.

Exit code 2 with stderr → teammate continues working instead of idling.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

STATE_ROOT = Path.home() / ".claude" / "state"

BYPASS_TAG = "[silent-close]"

# A human DM carrying this tag is a quick steer, not a conversational
# takeover — it doesn't count as a user DM, so the lead-nag still fires.
STEER_TAG = "[steer]"

# Reuse the tracker's reconcilers so we don't gate idle on stale entries
# (mid-tool-call task-notifications, fired-but-uncleared crons).
sys.path.insert(0, str(Path(__file__).parent))
try:
    from inflight_tracker import sweep_state_from_transcript, gc_crons
except Exception:
    sweep_state_from_transcript = None
    gc_crons = None

TEAMMATE_MSG_PATTERN = re.compile(
    r'<teammate-message[^>]*teammate_id="([^"]+)"[^>]*>\n?(.*?)\n?</teammate-message>',
    re.DOTALL,
)


def has_inflight(session_id: str, transcript_path: str) -> bool:
    p = STATE_ROOT / session_id / "inflight.json"
    if not p.exists():
        return False
    try:
        state = json.loads(p.read_text())
    except json.JSONDecodeError:
        return False
    # Opportunistic reconcile: stale entries (mid-call notifications,
    # fired-but-uncleared crons) shouldn't silently gate the nag.
    if sweep_state_from_transcript and transcript_path:
        sweep_state_from_transcript(state, transcript_path)
    if gc_crons:
        gc_crons(state)
    return any(state.get(k) for k in ("bash_bg", "agents", "monitor", "crons"))


def is_protocol_text(text: str) -> bool:
    """True if text parses to JSON dict with a `type` field."""
    if not isinstance(text, str):
        return False
    try:
        parsed = json.loads(text.strip())
    except (json.JSONDecodeError, ValueError):
        return False
    return isinstance(parsed, dict) and "type" in parsed


def first_lead_block_text(content: str) -> str | None:
    """Inner text of the first team-lead <teammate-message> in content, or None."""
    if not isinstance(content, str) or "<teammate-message" not in content:
        return None
    for m in TEAMMATE_MSG_PATTERN.finditer(content):
        if m.group(1) == "team-lead":
            return m.group(2)
    return None


def is_user_dm_entry(entry: dict) -> bool:
    """True if this transcript entry is a real direct user DM (not a teammate-msg, not a task-notification, not meta, not a [steer])."""
    if entry.get("type") != "user":
        return False
    if entry.get("isMeta"):
        return False
    if (entry.get("origin") or {}).get("kind") == "task-notification":
        return False
    content = entry.get("message", {}).get("content")
    if not isinstance(content, str):
        return False
    if "<teammate-message" in content:
        return False
    if "<task-notification" in content:
        return False
    # A [steer] DM is a quick course-correction, not a takeover — don't let it
    # suppress the lead-nag.
    if STEER_TAG in content:
        return False
    return bool(content.strip())


def send_to_lead_ids(entry: dict) -> list[str]:
    """tool_use ids of SendMessage(to='team-lead', non-protocol) calls in this assistant entry."""
    if entry.get("type") != "assistant":
        return []
    content = entry.get("message", {}).get("content")
    if not isinstance(content, list):
        return []
    ids = []
    for c in content:
        if not isinstance(c, dict):
            continue
        if c.get("type") != "tool_use" or c.get("name") != "SendMessage":
            continue
        inp = c.get("input", {})
        if inp.get("to") != "team-lead":
            continue
        msg = inp.get("message")
        if isinstance(msg, str) and not is_protocol_text(msg) and c.get("id"):
            ids.append(c["id"])
    return ids


def tool_result_outcomes(entry: dict):
    """Yield (tool_use_id, is_error) for each tool_result in this user entry."""
    if entry.get("type") != "user":
        return
    content = entry.get("message", {}).get("content")
    if not isinstance(content, list):
        return
    for c in content:
        if isinstance(c, dict) and c.get("type") == "tool_result":
            tid = c.get("tool_use_id")
            if tid:
                yield tid, bool(c.get("is_error"))


def has_silent_close(entry: dict) -> bool:
    """True if this assistant entry's text contains the deliberate-silence tag."""
    if entry.get("type") != "assistant":
        return False
    content = entry.get("message", {}).get("content")
    if isinstance(content, str):
        return BYPASS_TAG in content
    if isinstance(content, list):
        return any(
            BYPASS_TAG in c.get("text", "")
            for c in content
            if isinstance(c, dict) and c.get("type") == "text"
        )
    return False


def analyze_transcript(transcript_path: Path) -> tuple[bool, bool, bool]:
    """Return (has_lead_msg, has_reply_to_lead, has_user_dm) for the relevant window."""
    if not transcript_path.exists():
        return False, False, False

    entries: list[dict] = []
    with transcript_path.open() as f:
        for line in f:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    # Walk back to find the most recent non-protocol lead message.
    last_lead_idx = None
    for i in range(len(entries) - 1, -1, -1):
        e = entries[i]
        if e.get("type") != "user":
            continue
        body = first_lead_block_text(e.get("message", {}).get("content"))
        if body is None or is_protocol_text(body):
            continue
        last_lead_idx = i
        break

    if last_lead_idx is None:
        return False, False, False

    has_reply = False
    has_user_dm = False
    already_nagged = False
    # A send to the lead only counts once its tool_result comes back without
    # is_error: a rejected SendMessage (e.g. missing `summary`) leaves the same
    # tool_use block in the transcript but never reached the lead. Track sends
    # by id, then mark has_reply when a non-errored result for one shows up.
    pending_send_ids: set[str] = set()
    for e in entries[last_lead_idx + 1 :]:
        pending_send_ids.update(send_to_lead_ids(e))
        for tid, is_error in tool_result_outcomes(e):
            if tid in pending_send_ids and not is_error:
                has_reply = True
        if has_silent_close(e):
            has_reply = True
        if is_user_dm_entry(e):
            has_user_dm = True
        # Detect a prior nag (this hook's META entry) since the last lead msg.
        if e.get("type") == "user" and e.get("isMeta"):
            content = e.get("message", {}).get("content")
            if isinstance(content, str) and "teammate_idle_nag.py" in content:
                already_nagged = True

    return True, has_reply or already_nagged, has_user_dm


def main() -> None:
    data = json.load(sys.stdin)
    if data.get("hook_event_name") != "TeammateIdle":
        return

    teammate_name = data.get("teammate_name", "")
    if not teammate_name or teammate_name == "team-lead":
        return

    session_id = data.get("session_id", "")
    transcript_path = Path(data.get("transcript_path", ""))

    if has_inflight(session_id, str(transcript_path)):
        return

    has_lead, has_reply, has_user_dm = analyze_transcript(transcript_path)
    if not has_lead or has_reply:
        return

    # Human is actively conversing with the teammate (DM'd since the last lead
    # message) — their DM supersedes the lead's claim on attention, so stand
    # down. A fresh lead message re-anchors `last_lead_idx` past this DM and
    # nagging resumes normally; the suppression is per-message, not permanent.
    if has_user_dm:
        return

    msg = (
        "You received a message from team-lead and haven't replied via SendMessage. "
        "Send a reply before idling — even a brief 'noted' or 'on it' so the lead knows you saw it. "
        "If the lead's message was purely informational and needs no real response, a quick "
        "acknowledgement is still expected. If you deliberately chose silence (exchange already "
        f"closed on both sides, a reply would be noise), put `{BYPASS_TAG}` in your final text "
        "and this hook will stand down."
    )
    print(msg, file=sys.stderr)
    sys.exit(2)


if __name__ == "__main__":
    main()
