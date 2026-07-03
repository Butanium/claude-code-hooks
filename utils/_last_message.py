"""Transcript reading utilities for hooks.

Copied from ~/automation/claude-lab/plugins/clab/hooks/_transcript_utils.py.
TODO: consolidate into a shared claude-hooks-helper package
(see ~/automation/claude-hooks-helper/TODO.md).
"""
import json
import os
import tempfile
import time
import random
import string


IDLE_MARKER = "[PASS]"

# Shape signal: CLI-internal metadata entries (e.g. last-prompt, ai-title,
# agent-setting, permission-mode, bridge-session, attachment,
# file-history-snapshot, system/stop_hook_summary, system/turn_duration)
# never carry a `message` field. Only user/assistant turns do. So
# "no message field" is a reliable marker that we're past any in-progress
# assistant turn.


def _decode_chunk(raw: bytes) -> tuple[str, bytes]:
    """Decode bytes to str, handling splits in the middle of multi-byte UTF-8 chars.

    Returns (decoded_text, leftover_bytes) where leftover_bytes are the leading
    bytes that couldn't be decoded (up to 3 bytes from a split multi-byte char).
    These should be prepended to the next chunk read from the left.
    """
    try:
        return raw.decode(), b""
    except UnicodeDecodeError:
        # The chunk boundary split a multi-byte UTF-8 character.
        # UTF-8 chars are at most 4 bytes, so skip up to 3 leading continuation bytes.
        for i in range(1, 4):
            try:
                return raw[i:].decode(), raw[:i]
            except UnicodeDecodeError:
                continue
        # Should not happen with valid UTF-8 files, but fail loudly if it does
        raise


def get_last_jsonl_entry(transcript_path: str) -> dict | None:
    """Return the last non-progress JSONL entry as a dict.

    Reads from the end of the file for efficiency. Skips 'progress' entries
    since those are written by hooks themselves.
    """
    with open(transcript_path, "rb") as f:
        f.seek(0, 2)
        position = f.tell()
        remainder = ""
        chunk_size = 8192
        while position > 0:
            read_size = min(chunk_size, position)
            position -= read_size
            f.seek(position)
            text, leftover = _decode_chunk(f.read(read_size))
            # If we split a multi-byte char, those leading bytes belong to the
            # chunk further left — adjust position so they're re-read next time.
            position += len(leftover)
            chunk = text + remainder
            lines = chunk.splitlines(True)
            # First line may be partial (split across chunks), save for next iteration
            remainder = lines[0] if position > 0 else ""
            complete_lines = lines[1:] if position > 0 else lines
            for line in reversed(complete_lines):
                stripped = line.strip()
                if not stripped:
                    continue
                obj = json.loads(stripped)
                if obj.get("type") != "progress":
                    return obj
    return None


def _summarize_entry(entry: dict) -> dict:
    """Minimal summary of a transcript entry for debug logging."""
    t = entry.get("type")
    msg = entry.get("message", {})
    summary = {"type": t, "ts": entry.get("timestamp", "")}
    if subtype := entry.get("subtype"):
        summary["subtype"] = subtype
    if model := msg.get("model"):
        summary["model"] = model
    content = msg.get("content", [])
    if isinstance(content, list):
        texts = [c.get("text", "")[:80] for c in content if c.get("type") == "text" and c.get("text", "").strip()]
        if texts:
            summary["texts"] = texts
    return summary


def _get_last_n_entries(transcript_path: str, n: int = 4) -> list[dict]:
    """Return the last n non-progress entries from the transcript (from the end)."""
    entries = []
    with open(transcript_path, "rb") as f:
        f.seek(0, 2)
        position = f.tell()
        remainder = ""
        chunk_size = 8192
        while position > 0 and len(entries) < n:
            read_size = min(chunk_size, position)
            position -= read_size
            f.seek(position)
            text, leftover = _decode_chunk(f.read(read_size))
            position += len(leftover)
            chunk = text + remainder
            lines = chunk.splitlines(True)
            remainder = lines[0] if position > 0 else ""
            complete_lines = lines[1:] if position > 0 else lines
            for line in reversed(complete_lines):
                stripped = line.strip()
                if not stripped:
                    continue
                obj = json.loads(stripped)
                if obj.get("type") != "progress":
                    entries.append(obj)
                    if len(entries) >= n:
                        break
    return entries


def wait_for_transcript_flush(transcript_path: str, debug_dir: str | None = None) -> bool:
    """Poll until the last JSONL entry has message.model (assistant turn flushed).

    Exponential backoff: 50ms, 100ms, 200ms, ... up to ~12.75s total.
    Returns True if flush detected, False if timed out.

    Forensic logging: if any retry occurs (first poll didn't succeed), writes a
    summary JSON to {tempdir}/wait_for_transcript_flush_debug/ on completion.
    Captures per-iteration state + a tail snapshot so pathological tails (e.g.
    CLI-internal markers like last-prompt/agent-setting/permission-mode landing
    after the assistant turn) can be diagnosed without rerunning live.
    """
    t_start = time.time()
    iterations: list[dict] = []
    wait = 0.05
    success = False
    while wait <= 8:
        last_entry = get_last_jsonl_entry(transcript_path)
        has_model = bool(last_entry and last_entry.get("message", {}).get("model"))
        is_cli_marker = bool(last_entry and "message" not in last_entry)
        is_flushed = has_model or is_cli_marker

        iterations.append({
            "elapsed_s": round(time.time() - t_start, 3),
            "wait_s": wait,
            "has_model": has_model,
            "is_cli_marker": is_cli_marker,
            "last_entry": _summarize_entry(last_entry) if last_entry else None,
        })

        if debug_dir:
            rand_suffix = ''.join(random.choices(string.ascii_lowercase, k=6))
            os.makedirs(debug_dir, exist_ok=True)
            last_4 = _get_last_n_entries(transcript_path, 4)
            with open(f"{debug_dir}/{int(time.time())}_{rand_suffix}_poll.json", "w") as f:
                json.dump({
                    "wait_s": wait,
                    "has_model": has_model,
                    "is_cli_marker": is_cli_marker,
                    "last_4": [_summarize_entry(e) for e in last_4],
                }, f, indent=2)

        if is_flushed:
            success = True
            break

        time.sleep(wait)
        wait *= 2

    if len(iterations) > 1:
        try:
            forensic_dir = os.path.join(tempfile.gettempdir(), "wait_for_transcript_flush_debug")
            os.makedirs(forensic_dir, exist_ok=True)
            rand = ''.join(random.choices(string.ascii_lowercase, k=6))
            log_path = os.path.join(forensic_dir, f"{int(time.time())}_{rand}.json")
            with open(log_path, "w") as f:
                json.dump({
                    "transcript_path": transcript_path,
                    "succeeded": success,
                    "total_elapsed_s": round(time.time() - t_start, 3),
                    "iterations_count": len(iterations),
                    "iterations": iterations,
                    "tail_snapshot": [_summarize_entry(e) for e in _get_last_n_entries(transcript_path, 5)],
                }, f, indent=2)
        except OSError:
            pass

    return success


def count_assistant_entries_from_offset(transcript_path: str, byte_offset: int) -> int:
    """Count assistant-type entries in the transcript after the given byte offset."""
    count = 0
    with open(transcript_path, "rb") as f:
        f.seek(byte_offset)
        for line in f:
            stripped = line.strip()
            if not stripped:
                continue
            obj = json.loads(stripped)
            if obj.get("type") == "assistant":
                count += 1
    return count


def get_last_assistant_text(transcript_path: str) -> str:
    """Read transcript JSONL and return the last assistant message text."""
    last_text = ""
    with open(transcript_path) as f:
        for line in f:
            obj = json.loads(line)
            if obj.get("type") != "assistant":
                continue
            msg = obj.get("message", {})
            content = msg.get("content", "")
            if isinstance(content, list):
                text = " ".join(
                    c.get("text", "") for c in content if c.get("type") == "text"
                )
            else:
                text = str(content)
            if text.strip():
                last_text = text.strip()
    return last_text


# Prefixes that mark a type=="user" entry as NOT a genuine human turn.
# - <teammate-message>: a real message, but from another Claude instance (team
#   lead / peer teammate), not the human.
# - <task-notification>: not a message at all — a completed-background-work event
#   injected as a user entry. It must be *walked past* (it shouldn't mask the
#   actual last message), which is what get_last_user_text(skip_task_notifications=True)
#   does — whereas a teammate message is genuinely the last thing said and is not
#   skipped.
TEAMMATE_MSG_PREFIX = "<teammate-message"
TASK_NOTIFICATION_PREFIX = "<task-notification"
NON_HUMAN_USER_PREFIXES = (TEAMMATE_MSG_PREFIX, TASK_NOTIFICATION_PREFIX)


def is_human_turn(user_text: str) -> bool:
    """True if a user-message text is a genuine human turn (not a teammate/peer
    message or a task-notification). Pass text from get_last_user_text()."""
    return bool(user_text) and not user_text.lstrip().startswith(NON_HUMAN_USER_PREFIXES)


def get_last_user_text(transcript_path: str, skip_task_notifications: bool = False) -> str:
    """Read transcript JSONL and return the last user message text.

    Task-notifications (completed-background-work events) are injected as
    type=="user" entries. With skip_task_notifications=True they're walked past,
    so the result is the last *genuine* message (human or teammate) rather than a
    notification that merely happens to be the most recent user entry — otherwise
    an important human message that a notification landed after gets masked.
    """
    last_text = ""
    with open(transcript_path) as f:
        for line in f:
            obj = json.loads(line)
            if obj.get("type") != "user":
                continue
            msg = obj.get("message", {})
            content = msg.get("content", "")
            if isinstance(content, list):
                text = " ".join(
                    c.get("text", "") for c in content if c.get("type") == "text"
                )
            else:
                text = str(content)
            text = text.strip()
            if not text:
                continue
            if skip_task_notifications and text.startswith(TASK_NOTIFICATION_PREFIX):
                continue
            last_text = text
    return last_text
