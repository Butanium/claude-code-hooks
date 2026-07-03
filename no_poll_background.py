#!/usr/bin/env python3
"""PreToolUse(Read|Bash) guard: block the doom-loop poll — reading a background
task's output file in a tight no-yield streak before the task has finished.

A read of a live task's output is the bad poll ONLY when BOTH are true: the task
hasn't finished AND the agent never stopped (kept streaking tool_use). So allow the
read if EITHER "it's fine to read now" signal holds:

  (a) the task's completion <task-notification> is in the transcript — the task
      terminated, so the output file is final; OR
  (b) the assistant has yielded (stop_reason == "end_turn") at least once since
      launching the task — it's not in the tight poll loop this guard exists to
      break, so a deliberate read is fine even if (a) wasn't detected.

Deny only when NEITHER holds (task launched in this transcript, no notification yet,
no end_turn since launch) — that's the actual doom-loop.

Why both arms: notifications flush at tool_use boundaries, not only after end_turn, so
an end_turn-only check denied legitimate post-notification reads by a streaking agent
(found by dogfooding, 2026-06-23). Arm (a) fixes that. Arm (b) keeps the original
intent — a yielded agent isn't doom-looping — and makes the guard robust if the
notification format ever changes.

Two read surfaces are guarded:
  - Read tool on the task's output file.
  - Bash command that dumps/inspects the file (`cat`, `tail`, `grep`, …). The
    command must START with one of those (after leading whitespace), so a deliberate
    wait-then-read like `sleep 30; cat <file>` — not an immediate poll — isn't
    matched.

Failure policy — this hook does NOT silently fail open. Malformed hook input or an
unreadable / corrupt transcript raise: the traceback goes to stderr and the script
exits 1, a *non-blocking* hook error (surfaced to the user; the call still proceeds —
exit 2 is the only blocking code). A broken guard is loud but never strands a call it
can't reason about. The only quiet pass-throughs are genuinely-not-applicable cases
(not a guarded read, not a task output file, no transcript, launch absent) — those
aren't failures, so they allow.

Opt-out — include the literal tag [NOT-IN-A-DOOM-READ-LOOP] in the message issuing
the call (or, for Bash, as a comment in the command) to deliberately peek at a
still-partial file (e.g. debugging *why* a background job is stuck).
"""
import json
import json as _json
import re
import sys

# Background task output files: /tmp/.../<session>/tasks/<taskId>.output
# Non-anchored: matched against a Read file_path AND inside a Bash command string.
TASK_PATH_RE = re.compile(r"/tasks/([A-Za-z0-9]+)\.output")

# Bash commands that dump/inspect a file's contents — a poll when aimed at a live
# task output file. The command must START with one of these (after leading
# whitespace); a chained `sleep …; cat …` therefore does NOT match (the leading
# token is `sleep`, not a reader). Easily extended — add readers here.
BASH_READ_CMD_RE = re.compile(
    r"\s*(?:cat|tac|head|tail|less|more|bat|nl|grep|egrep|fgrep|rg|awk|sed|xxd|od"
    r"|strings|wc)(?:\s|$)"
)

BYPASS_TAG = "[NOT-IN-A-DOOM-READ-LOOP]"


def _prior_deny_for_task(records, task_id):
    """True if an earlier record already carried a no_poll deny for THIS task.
    We only reach this after arm-(b) confirmed no end_turn since launch, so a
    prior deny means the model re-polled without ever yielding => force-stop.
    The ONLY thing that resets the escalation is a real end_turn (handled by
    arm-(b) upstream); ScheduleWakeup, sleep;echo, and re-reads in between are
    all failed yields and do NOT reset it."""
    deny_marker = "hasn't sent its completion <task-notification>"
    for rec in records[:-1]:  # exclude the current read tool_use (last record)
        if _is_assistant(rec):
            continue
        d = _json.dumps(rec)
        if deny_marker in d and task_id in d:
            return True
    return False


def _stop_reason(rec: dict):
    msg = rec.get("message")
    if isinstance(msg, dict) and "stop_reason" in msg:
        return msg.get("stop_reason")
    return rec.get("stop_reason")


def _is_assistant(rec: dict) -> bool:
    if rec.get("type") == "assistant":
        return True
    msg = rec.get("message")
    return isinstance(msg, dict) and msg.get("role") == "assistant"


def _assistant_text(rec: dict) -> str:
    """Concatenate the text blocks of an assistant record, for tag scanning."""
    msg = rec.get("message")
    if not isinstance(msg, dict) or msg.get("role") != "assistant":
        return ""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        block.get("text", "")
        for block in content
        if isinstance(block, dict) and block.get("type") == "text"
    )


def _task_id(tool_name: str, tool_input: dict):
    """Task id this call would read from a task-output file, or None if it isn't one.

    Read: the file_path IS a task output file.
    Bash: the command STARTS with a content-dumping reader AND references a task
          output path. The leading-reader gate is what makes `sleep …; cat …`,
          writes, and "mentions the path mid-pipeline" not match.
    """
    if tool_name == "Read":
        m = TASK_PATH_RE.search(tool_input.get("file_path") or "")
        return m.group(1) if m else None
    if tool_name == "Bash":
        command = tool_input.get("command") or ""
        if not BASH_READ_CMD_RE.match(command):
            return None
        m = TASK_PATH_RE.search(command)
        return m.group(1) if m else None
    return None


def main() -> None:
    # No try/except: a malformed payload is a real bug — let it crash loudly.
    data = json.load(sys.stdin)

    tool_name = data.get("tool_name")
    if tool_name not in ("Read", "Bash"):
        return
    tool_input = data.get("tool_input") or {}
    task_id = _task_id(tool_name, tool_input)
    if task_id is None:
        return  # not a read of a task output file — guard doesn't apply

    transcript = data.get("transcript_path")
    if not transcript:
        return  # no transcript to reason over — guard doesn't apply

    # No try/except: transcript_path was provided, so an unreadable or corrupt
    # transcript is an anomaly worth seeing, not silently swallowing.
    with open(transcript) as f:
        records = [json.loads(line) for line in f if line.strip()]
    dumps = [json.dumps(rec) for rec in records]

    # ARM (a) — has this task's completion notification already landed? That's the
    # direct signal the output file is final. Match the <task-notification> block
    # carrying this id, and only in NON-assistant records — so an agent that merely
    # *quotes* a notification in its prose (like this file's docstring) can't spoof it.
    notif_marker = f"<task-id>{task_id}</task-id>"
    if any(
        not _is_assistant(rec) and "<task-notification>" in d and notif_marker in d
        for rec, d in zip(records, dumps)
    ):
        return  # task notified ⇒ output is final ⇒ allow this read

    # Locate the launch: the FIRST record (excluding the current call record, whose
    # path self-matches the id) that mentions this id — the bg tool_result
    # "Command running in background with ID: <id> … <id>.output". Absent ⇒ task is
    # from a prior/compacted session ⇒ can't reason ⇒ fail open (allow).
    launch_i = next((i for i, d in enumerate(dumps[:-1]) if task_id in d), None)
    if launch_i is None:
        return

    # ARM (b) — has the assistant yielded (end_turn) at least once since launching it?
    # A yield means it's not in the tight no-stop poll loop this guard breaks, so a
    # deliberate read is fine.
    if any(_stop_reason(rec) == "end_turn" for rec in records[launch_i + 1:]):
        return

    # ESCALATION (V2): re-poll without ever yielding => force-stop the whole turn.
    if _prior_deny_for_task(records, task_id):
        print(json.dumps({
            "continue": False,
            "stopReason": (
                f"FORCED-STOP-NO-POLL: you re-read the still-running task {task_id} "
                "immediately after being denied, with no yield in between. The turn has "
                "been force-ended so you stop looping. You'll be woken when the task "
                "completes."
            ),
        }))
        return

    # Neither arm: launched here, no notification, no yield ⇒ the doom-loop poll ⇒ deny.
    # Deliberate override: the tag in the command (Bash comment) or in the prose of the
    # assistant turn issuing this call.
    command = tool_input.get("command") or ""
    if BYPASS_TAG in command or (records and BYPASS_TAG in _assistant_text(records[-1])):
        return

    reason = (
        f"Task {task_id} hasn't sent its completion <task-notification> yet AND you "
        f"haven't yielded the turn since launching it — so it's still running, its "
        f"output file is partial, and you're in a tight read loop. Reading it now shows "
        f"nothing useful. End the turn (ending=finishing your turn and NOT doing any tool calls) or do unrelated work; "
        f"you'll be woken when it completes (a backup watchdog is fine). If you REALLY "
        f"need to peek at the partial file now (you really should just idle and wait for "
        f"the notification instead of cluttering your context), include {BYPASS_TAG} in "
        f"your message."
    )
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))


if __name__ == "__main__":
    main()
