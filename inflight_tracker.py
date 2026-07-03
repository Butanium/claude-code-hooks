#!/usr/bin/env python3
"""Multi-event hook tracking in-flight background work per session.

State file: ~/.claude/state/<session-id>/inflight.json
Kinds tracked: bash_bg, agents, monitor, crons.

Event dispatch (via `hook_event_name`):
- PostToolUse: parse tool_response for IDs, add entries tagged with the
  owning Claude process's pid + procStart (so liveness can be checked
  on session start). CronDelete removes by cron_id.
- UserPromptSubmit: parse incoming prompt for terminal <task-notification>
  blocks (those with <status>), remove matched task-ids.
- SessionStart: garbage-collect entries whose owning Claude process is
  no longer alive (or whose procStart no longer matches → PID reuse).
  Crons are intentionally NOT gc'd this way — recurring/durable crons
  can outlive the Claude that scheduled them.

Designed as a quiet bookkeeper — emits no permissionDecision; only
side-effects the state file. Other hooks (e.g., TeammateIdle nag)
consume the state.

Multiple Claude processes can share a session_id (across resume or
concurrent `claude --continue`), so writes go through a tmp+rename
to avoid torn reads.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

STATE_ROOT = Path.home() / ".claude" / "state"

TEXT_ID_PATTERNS = {
    "Monitor":    (re.compile(r"Monitor started \(task ([a-zA-Z0-9]+),"), "monitor"),
    "CronCreate": (re.compile(r"Scheduled (?:one-shot|recurring) task ([a-zA-Z0-9]+)"), "crons"),
}
TOOL_KIND = {
    "Bash":       "bash_bg",
    "Agent":      "agents",
    "Monitor":    "monitor",
    "CronCreate": "crons",
}

EMPTY_STATE = {"bash_bg": [], "agents": [], "monitor": [], "crons": []}

# Kinds whose entries are tagged with the owning Claude pid+procStart and
# gc'd when that process dies. Crons are excluded — they can survive resume.
PROCESS_BOUND_KINDS = ("bash_bg", "agents", "monitor")


def state_path(session_id: str) -> Path:
    return STATE_ROOT / session_id / "inflight.json"


def read_state(session_id: str) -> dict:
    p = state_path(session_id)
    if not p.exists():
        return {k: [] for k in EMPTY_STATE}
    try:
        s = json.loads(p.read_text())
    except json.JSONDecodeError:
        return {k: [] for k in EMPTY_STATE}
    for k in EMPTY_STATE:
        s.setdefault(k, [])
    return s


def write_state(session_id: str, state: dict) -> None:
    """Atomic write: serialize to <file>.tmp, then rename over the real path.

    Multiple Claude processes sharing a session_id can race on this file.
    The tmp+rename ensures readers see either the old or the new file
    intact, never a torn write.
    """
    p = state_path(session_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(p)


def tool_response_text(tr) -> str:
    """Extract text from tool_response (which may be a str, dict, or list-of-text-blocks)."""
    if isinstance(tr, str):
        return tr
    if isinstance(tr, dict):
        c = tr.get("content")
        if isinstance(c, str):
            return c
        if isinstance(c, list):
            return " ".join(x.get("text", "") for x in c if isinstance(x, dict) and x.get("type") == "text")
    return ""


try:
    import psutil  # type: ignore
except ImportError:
    psutil = None  # GC will be a no-op without psutil


def read_proc_start(pid: int) -> str:
    """Return process creation time as a string, used to detect PID reuse.

    Falls back to "" if psutil isn't available or the process is gone.
    The exact value isn't meaningful — only equality comparison is used.
    """
    if psutil is None:
        return ""
    try:
        return f"{psutil.Process(pid).create_time():.6f}"
    except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
        return ""


def find_claude_ancestor(start_pid: int = None) -> tuple[int, str] | None:
    """Walk up the process tree from `start_pid` (default: own ppid) until
    a process whose `name()` is `claude` is found. Returns (pid, procStart)
    or None if no claude ancestor is found within 20 hops.
    """
    if psutil is None:
        return None
    try:
        proc = psutil.Process(start_pid) if start_pid is not None else psutil.Process(os.getppid())
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None
    for _ in range(20):
        try:
            name = proc.name()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None
        if name == "claude":
            return proc.pid, f"{proc.create_time():.6f}"
        try:
            proc = proc.parent()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            return None
        if proc is None or proc.pid <= 1:
            return None
    return None


def is_claude_alive(claude_pid: int, claude_procStart: str) -> bool:
    """True iff `claude_pid` is alive AND its current procStart matches
    `claude_procStart` (or the stored procStart was empty, in which case
    we trust the pid match alone)."""
    if psutil is None:
        # Can't verify — conservatively report alive to avoid wrongly wiping.
        return True
    try:
        proc = psutil.Process(claude_pid)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    if not claude_procStart:
        return True
    try:
        current = f"{proc.create_time():.6f}"
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return False
    return current == claude_procStart


def handle_post_tool_use(data: dict, session_id: str) -> None:
    tool_name = data.get("tool_name", "")

    if tool_name == "CronDelete":
        tool_input = data.get("tool_input", {})
        cron_id = tool_input.get("cron_id") or tool_input.get("id", "")
        if not cron_id:
            return
        state = read_state(session_id)
        state["crons"] = [e for e in state["crons"] if e.get("id") != cron_id]
        write_state(session_id, state)
        return

    if tool_name == "TaskStop":
        # TaskStop kills a bg task without emitting a task-notification, so the
        # transcript sweep can't catch it. Remove the matching entry directly.
        tool_input = data.get("tool_input", {})
        task_id = tool_input.get("task_id") or tool_input.get("shell_id") or ""
        if not task_id:
            return
        state = read_state(session_id)
        for kind in PROCESS_BOUND_KINDS:
            state[kind] = [e for e in state[kind] if e.get("id") != task_id]
        write_state(session_id, state)
        return

    if tool_name not in TOOL_KIND:
        return

    tool_input = data.get("tool_input", {})
    if tool_name in ("Bash", "Agent") and not tool_input.get("run_in_background"):
        return

    tool_response = data.get("tool_response", {}) or {}
    kind = TOOL_KIND[tool_name]
    task_id = ""

    if tool_name == "Bash":
        task_id = tool_response.get("backgroundTaskId", "") if isinstance(tool_response, dict) else ""
    elif tool_name == "Agent":
        # Subagents (no team_name): agent_id is a hex string like "a8d8e7c..."
        # Teammate spawns (team_name set): agent_id like "name@team" — these are
        # persistent processes, NOT in-flight tasks that gate idle. Skip teammates.
        if tool_input.get("team_name"):
            return
        if isinstance(tool_response, dict):
            task_id = tool_response.get("agent_id") or tool_response.get("agentId") or ""
        if not task_id:
            text = tool_response_text(tool_response)
            m = re.search(r"agentId:\s*([a-zA-Z0-9]+)", text)
            if m:
                task_id = m.group(1)
    else:
        text = tool_response_text(tool_response)
        pattern, _ = TEXT_ID_PATTERNS[tool_name]
        m = pattern.search(text)
        if m:
            task_id = m.group(1)

    if not task_id:
        return

    state = read_state(session_id)
    if any(e.get("id") == task_id for e in state[kind]):
        return

    entry = {"id": task_id, "tool_use_id": data.get("tool_use_id", "")}
    if tool_name == "Bash":
        entry["command"] = (tool_input.get("command") or "")[:200]
    elif tool_name == "Agent":
        entry["description"] = (tool_input.get("description") or "")[:100]
        entry["agent_name"] = tool_input.get("name", "")
    elif tool_name == "Monitor":
        entry["description"] = (tool_input.get("description") or "")[:100]
    elif tool_name == "CronCreate":
        entry["cron"] = tool_input.get("cron", "")
        # CronCreate's recurring param defaults to True when omitted (per tool docs).
        entry["recurring"] = bool(tool_input.get("recurring", True))
        # Local-naive ISO. Crons fire in local time per the harness; storing
        # naive-local keeps croniter math straightforward.
        import datetime as _dt
        entry["created_at"] = _dt.datetime.now().isoformat()

    # Tag process-bound kinds with the owning Claude's pid+procStart so the
    # SessionStart GC can drop stale entries. Crons skip this — they can
    # outlive their creator Claude.
    if kind in PROCESS_BOUND_KINDS:
        ancestor = find_claude_ancestor()
        if ancestor is not None:
            entry["claude_pid"], entry["claude_procStart"] = ancestor

    state[kind].append(entry)

    # Reconcile on each add: transcript sweep for missed mid-tool-call
    # task-notifications, plus cron GC for fired/expired entries.
    sweep_state_from_transcript(state, data.get("transcript_path", ""))
    gc_crons(state)

    write_state(session_id, state)


TERMINAL_NOTIF_PATTERN = re.compile(r"<task-notification>(.*?)</task-notification>", re.DOTALL)
TASK_ID_PATTERN = re.compile(r"<task-id>([^<]+)</task-id>")
STATUS_PATTERN = re.compile(r"<status>([^<]+)</status>")


def gc_crons(state: dict) -> bool:
    """Drop cron entries whose fire-or-expire moment has passed.

    Rules:
      - One-shot (`recurring: false`): first fire time is
        `croniter(expr, start=created_at).get_next()`. If `now > first_fire + grace`,
        the harness has either fired or cancelled the cron — drop the entry.
      - Recurring (`recurring: true`): the harness auto-expires recurring crons
        7 days after creation. Drop entries older than that.

    Entries missing `created_at` (pre-this-change) are kept unconditionally
    — we can't make a safe decision without it.
    """
    try:
        import datetime as _dt
        from croniter import croniter
    except ImportError:
        return False

    crons = state.get("crons", [])
    if not crons:
        return False

    now = _dt.datetime.now()
    seven_days = _dt.timedelta(days=7)
    grace = _dt.timedelta(seconds=60)

    kept = []
    changed = False
    for e in crons:
        created_at_str = e.get("created_at")
        if not created_at_str:
            kept.append(e)
            continue
        try:
            created_at = _dt.datetime.fromisoformat(created_at_str)
        except ValueError:
            kept.append(e)
            continue

        if e.get("recurring"):
            if now - created_at > seven_days:
                changed = True
                continue
            kept.append(e)
            continue

        cron_expr = e.get("cron", "")
        if not cron_expr:
            kept.append(e)
            continue
        try:
            first_fire = croniter(cron_expr, start_time=created_at).get_next(_dt.datetime)
        except Exception:
            kept.append(e)
            continue
        if now > first_fire + grace:
            changed = True
            continue
        kept.append(e)

    if changed:
        state["crons"] = kept
    return changed


def sweep_state_from_transcript(state: dict, transcript_path: str) -> bool:
    """Walk the last ~400 entries of the recipient's transcript looking for
    terminal task-notifications (those with <status>). Remove matched task-ids
    from state. Returns True iff state changed.

    Task-notifications can land in several JSONL shapes:
      - inter-turn user msg (type=user, message.content="<task-notification>...")
      - mid-tool-call attachment (type=attachment, attachment.prompt="...")
      - internal queue-operation (type=queue-operation, content="...")

    Rather than match the schema, we regex the raw line for the XML — any
    occurrence of the block is good enough since the task-id is the handle.
    """
    if not transcript_path or not os.path.exists(transcript_path):
        return False
    from collections import deque

    try:
        with open(transcript_path, encoding="utf-8") as f:
            recent = list(deque(f, maxlen=400))
    except OSError:
        return False

    completed_ids = set()
    for line in recent:
        if "<task-notification>" not in line:
            continue
        # Lines are JSON-encoded so the inner XML appears with escaped quotes
        # and `\n`. The regex looks for the literal substring patterns; it
        # works against either escaped or unescaped forms.
        for m in TERMINAL_NOTIF_PATTERN.finditer(line):
            block = m.group(1)
            if not STATUS_PATTERN.search(block):
                continue
            tm = TASK_ID_PATTERN.search(block)
            if tm:
                completed_ids.add(tm.group(1).strip())

    if not completed_ids:
        return False
    changed = False
    for kind in EMPTY_STATE:
        kept = [e for e in state[kind] if e.get("id") not in completed_ids]
        if len(kept) != len(state[kind]):
            state[kind] = kept
            changed = True
    return changed


def handle_user_prompt_submit(data: dict, session_id: str) -> None:
    prompt = data.get("prompt", "")
    if not isinstance(prompt, str) or "<task-notification>" not in prompt:
        return

    completed_ids = set()
    for m in TERMINAL_NOTIF_PATTERN.finditer(prompt):
        block = m.group(1)
        if not STATUS_PATTERN.search(block):
            continue
        tm = TASK_ID_PATTERN.search(block)
        if tm:
            completed_ids.add(tm.group(1).strip())

    if not completed_ids:
        return

    state = read_state(session_id)
    changed = False
    for kind in EMPTY_STATE:
        kept = [e for e in state[kind] if e.get("id") not in completed_ids]
        if len(kept) != len(state[kind]):
            state[kind] = kept
            changed = True
    if changed:
        write_state(session_id, state)


def handle_session_start(data: dict, session_id: str) -> None:
    """GC entries whose owning Claude process is dead (or whose procStart
    no longer matches, indicating PID reuse). Crons are left alone — they
    can survive resume. Also opportunistically reconciles from the
    transcript to catch task-notifications we may have missed."""
    p = state_path(session_id)
    if not p.exists():
        return
    state = read_state(session_id)
    changed = sweep_state_from_transcript(state, data.get("transcript_path", ""))
    if gc_crons(state):
        changed = True
    for kind in PROCESS_BOUND_KINDS:
        kept = []
        for e in state[kind]:
            cpid = e.get("claude_pid")
            if cpid is None:
                # Untagged (e.g., entry from before this design). Keep —
                # we cannot verify, prefer false-positives over wrongly
                # wiping a real running task.
                kept.append(e)
                continue
            cps = e.get("claude_procStart", "") or ""
            if is_claude_alive(int(cpid), cps):
                kept.append(e)
            # else: drop
        if len(kept) != len(state[kind]):
            state[kind] = kept
            changed = True
    if changed:
        write_state(session_id, state)


def main() -> None:
    data = json.load(sys.stdin)
    session_id = data.get("session_id", "")
    if not session_id:
        return
    event = data.get("hook_event_name", "")
    if event == "PostToolUse":
        handle_post_tool_use(data, session_id)
    elif event == "UserPromptSubmit":
        handle_user_prompt_submit(data, session_id)
    elif event == "SessionStart":
        handle_session_start(data, session_id)


if __name__ == "__main__":
    main()
