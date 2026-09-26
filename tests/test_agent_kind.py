#!/usr/bin/env python3
"""utils/_agent_kind.is_subagent + the subagent branch of force_background_bash.py.

In-process teammates carry a bare agent_id like subagents; only the CLI's
subagents/agent-<id>.meta.json (taskKind) tells them apart.

Run: python3 tests/test_agent_kind.py
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from utils._agent_kind import is_subagent  # noqa: E402

os.environ["FORCE_BACKGROUND_BASH_CLAUDE_BIN"] = "/nonexistent/claude"


def check(label, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        sys.exit(1)


tmp = Path(tempfile.mkdtemp())
proj = tmp / "-home-x-proj"
sid = "11111111-2222-3333-4444-555555555555"
sub = proj / sid / "subagents"
sub.mkdir(parents=True)
transcript = proj / f"{sid}.jsonl"
transcript.write_text("")
(sub / "agent-awhowas-reviewer-33301bc6b51924bf.meta.json").write_text(
    json.dumps({"agentType": "whowas-reviewer", "taskKind": "in_process_teammate"}))
(sub / "agent-a56320ef901c39201.meta.json").write_text(json.dumps({"agentType": "general-purpose"}))


def payload(agent_id=None, tp=transcript, **tool_input):
    d = {"tool_name": "Bash", "session_id": sid, "transcript_path": str(tp),
         "tool_input": {"command": "echo hi", **tool_input}}
    if agent_id is not None:
        d["agent_id"] = agent_id
    return d


check("main agent: not a subagent", not is_subagent(payload()))
check("team member name@team: not a subagent", not is_subagent(payload("bob@session-1")))
check("in-process teammate (meta taskKind): not a subagent",
      not is_subagent(payload("awhowas-reviewer-33301bc6b51924bf")))
check("plain subagent (meta without taskKind): subagent", is_subagent(payload("a56320ef901c39201")))
check("bare id, no meta file: subagent (old rule)", is_subagent(payload("a0000000000000000")))
check("bare id, no transcript_path: subagent (old rule)",
      is_subagent({"agent_id": "a0000000000000000", "tool_input": {}}))
own = sub / "agent-awhowas-reviewer-33301bc6b51924bf.jsonl"
own.write_text("")
check("transcript_path = the agent's own transcript: meta beside it",
      not is_subagent(payload("awhowas-reviewer-33301bc6b51924bf", tp=own)))
(sub / "agent-abad.meta.json").write_text("{not json")
check("corrupt meta: subagent (old rule), no crash", is_subagent(payload("abad")))


def run_bash_hook(p):
    res = subprocess.run([sys.executable, str(ROOT / "force_background_bash.py")], input=json.dumps(p),
                         capture_output=True, text=True, cwd=ROOT)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout) if res.stdout.strip() else None


# 2.1.280: subagents are re-invoked by their own task notifications; idle in-process
# teammates are not (their notifications wait in the lead's queue).
check("e2e: subagent run_in_background allowed",
      run_bash_hook(payload("a56320ef901c39201", run_in_background=True)) is None)
out = run_bash_hook(payload("awhowas-reviewer-33301bc6b51924bf", run_in_background=True))
reason = (out or {}).get("hookSpecificOutput", {}).get("permissionDecisionReason", "")
check("e2e: in-process teammate run_in_background denied", out and out["hookSpecificOutput"]["permissionDecision"] == "deny")
check("e2e: deny names the in-process limit", "in-process teammate" in reason, reason)
check("e2e: in-process teammate with BACKGROUND_NEEDED allowed",
      run_bash_hook(payload("awhowas-reviewer-33301bc6b51924bf", run_in_background=True,
                            command="echo BACKGROUND_NEEDED && uv run server")) is None)
check("e2e: in-process teammate long sync timeout untouched",
      run_bash_hook(payload("awhowas-reviewer-33301bc6b51924bf", timeout=600000)) is None)
out = run_bash_hook(payload("a56320ef901c39201", timeout=120000))
check("e2e: subagent long sync timeout clamped like the main agent",
      (out or {}).get("hookSpecificOutput", {}).get("updatedInput", {}).get("timeout") == 60000, f"out={out}")
out = run_bash_hook(payload(timeout=120000))
hso = (out or {}).get("hookSpecificOutput", {})
check("e2e: main agent 120s sync -> clamped to 60s", hso.get("updatedInput", {}).get("timeout") == 60000, f"out={out}")
check("e2e: clamp is silent", "additionalContext" not in hso, f"out={out}")
check("e2e: 60s sync untouched", run_bash_hook(payload(timeout=60000)) is None)

res = subprocess.run([sys.executable, str(ROOT / "force_background_bash.py")], input="not json",
                     capture_output=True, text=True, cwd=ROOT)
check("e2e: malformed input -> non-blocking error, no deny", res.returncode == 1 and not res.stdout.strip(),
      f"rc={res.returncode}")

print("ALL OK")
