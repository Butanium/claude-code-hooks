"""Which tool really issued a PreToolUse call that reached a Bash hook?

Since Claude Code 2.1.314, hooks whose matcher names Bash also run on the shell
commands of the Monitor tool. The CLI hands them `tool_name: "Bash"` and a
`tool_input` cut down to `{command, description}`, and ignores their `allow` and
`updatedInput`: only deny and ask take effect. Nothing else in the hook input
says Monitor. The transcript's tool_use block, found by `tool_use_id`, does.

That row is written while the hook already runs: 25-110 ms after hook start on
2.1.321 (2026-11-09, -p session), so the lookup waits for it up to WAIT_S.
"""
import json
import time
from pathlib import Path

TAIL_BYTES = 1 << 20  # the call being checked is among the newest rows
WAIT_S = 2.0


def _transcripts(data: dict):
    tp = data.get("transcript_path")
    if not tp:
        return
    p = Path(tp)
    yield p
    agent_id = data.get("agent_id")
    if agent_id and p.parent.name != "subagents":
        yield p.parent / p.stem / "subagents" / f"agent-{agent_id}.jsonl"


def _tool_use_name(path: Path, tool_use_id: str) -> str | None:
    needle = json.dumps(tool_use_id).encode()
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - TAIL_BYTES))
            chunk = f.read()
            if needle not in chunk and size > TAIL_BYTES:
                f.seek(0)
                chunk = f.read()
    except OSError:
        return None
    for line in reversed(chunk.split(b"\n")):
        if needle not in line:
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            continue  # the tail's first line can be cut
        msg = rec.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        for b in content if isinstance(content, list) else ():
            if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id") == tool_use_id:
                return b.get("name")
    return None


def issuing_tool(data: dict) -> str | None:
    """Name of the tool_use block behind this hook call, or None if not found."""
    tool_use_id = data.get("tool_use_id")
    paths = list(_transcripts(data))
    if not tool_use_id or not paths:
        return None
    deadline = time.monotonic() + WAIT_S
    while True:
        for path in paths:
            name = _tool_use_name(path, tool_use_id)
            if name:
                return name
        if time.monotonic() >= deadline:
            return None
        time.sleep(0.025)


def is_monitor_call(data: dict) -> bool:
    return data.get("tool_name") == "Bash" and issuing_tool(data) == "Monitor"
