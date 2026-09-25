"""Is this hook call coming from a subagent (as opposed to the main agent or a teammate)?

Hook input carries `agent_id` only for calls made inside the lead's process:
- main agent, tmux/pane teammates: no `agent_id` (separate sessions).
- team members addressed as ``name@team``: an ``@`` in the id.
- subagents AND in-process teammates: a bare id (``a56320ef…`` or
  ``awhowas-reviewer-33301bc6…``). The two differ only in the CLI's sidecar
  ``subagents/agent-<id>.meta.json``, whose ``taskKind`` is
  ``"in_process_teammate"`` for the teammate. The old ``"@" not in agent_id``
  test read in-process teammates as subagents (2026-08-17, 08-28 denials).

When the sidecar can't be found or read, the bare-id rule stands (subagent).
"""
import json
from pathlib import Path


def _meta_candidates(data: dict, agent_id: str):
    name = f"agent-{agent_id}.meta.json"
    tp = data.get("transcript_path")
    if not tp:
        return
    p = Path(tp)
    if p.parent.name == "subagents":  # the subagent's own transcript
        yield p.parent / name
    yield p.parent / p.stem / "subagents" / name
    sid = data.get("session_id")
    if sid and sid != p.stem:
        yield p.parent / sid / "subagents" / name


def task_kind(data: dict) -> str | None:
    agent_id = data.get("agent_id") or ""
    if not agent_id:
        return None
    for meta in _meta_candidates(data, agent_id):
        try:
            return json.loads(meta.read_text()).get("taskKind") or ""
        except (OSError, ValueError):
            continue
    return None


def is_subagent(data: dict) -> bool:
    agent_id = data.get("agent_id") or ""
    if not agent_id or "@" in agent_id:
        return False
    return task_kind(data) != "in_process_teammate"
