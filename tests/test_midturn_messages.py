#!/usr/bin/env python3
"""midturn_messages.py: sender records, recipient injects once, idle repeat blocked.

Identity comes from name@team agent_ids here so no process lookup is involved;
the live two-teammate check is tests/smoke_midturn_messages.sh.

Run: python3 tests/test_midturn_messages.py
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "midturn_messages.py"
CFG = Path(tempfile.mkdtemp())
TEAM = CFG / "teams" / "t1"
(TEAM / "inboxes").mkdir(parents=True)
(TEAM / "config.json").write_text(json.dumps({"leadSessionId": "lead-sid", "members": [
    {"name": "team-lead"}, {"name": "rx"}, {"name": "tx"}]}))
ENV = {**os.environ, "CLAUDE_CONFIG_DIR": str(CFG)}


def check(label, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        sys.exit(1)


def run(payload):
    res = subprocess.run([sys.executable, str(HOOK)], input=json.dumps(payload) if not isinstance(payload, str)
                         else payload, capture_output=True, text=True, env=ENV)
    return res.returncode, (json.loads(res.stdout) if res.stdout.strip() else None)


def send(sender_agent, to, message, success=True):
    return run({"hook_event_name": "PostToolUse", "tool_name": "SendMessage", "session_id": "s-" + sender_agent,
                "agent_id": sender_agent, "tool_input": {"to": to, "message": message, "summary": "sum"},
                "tool_response": {"success": success, "routing": {"sender": sender_agent.split("@")[0],
                                                                  "target": "@" + to}}})


transcript = CFG / "rx.jsonl"
transcript.write_text("")


def tool(agent="rx@t1", sid="rx-sid"):
    return run({"hook_event_name": "PostToolUse", "tool_name": "Bash", "session_id": sid, "agent_id": agent,
                "transcript_path": str(transcript), "tool_input": {"command": "ls"}, "tool_response": {}})


def ups(prompt, agent="rx@t1", sid="rx-sid"):
    return run({"hook_event_name": "UserPromptSubmit", "session_id": sid, "agent_id": agent, "prompt": prompt})


def block(sender, text):
    return f'<teammate-message teammate_id="{sender}" summary="sum">\n{text}\n</teammate-message>'


rc, out = send("team-lead@t1", "rx", "STOP, the plan changed")
check("sender: exit 0, no output", rc == 0 and out is None)
chan = TEAM / "midturn" / "rx.jsonl"
check("sender: side channel written", chan.exists() and "STOP, the plan changed" in chan.read_text())
send("team-lead@t1", "rx", {"type": "shutdown_request"})
send("team-lead@t1", "rx", "failed send", success=False)
send("team-lead@t1", "nobody", "not a member")
check("sender: frames, failed sends, non-members skipped", len(chan.read_text().splitlines()) == 1
      and not (TEAM / "midturn" / "nobody.jsonl").exists())

rc, out = tool()
ctx = (out or {}).get("hookSpecificOutput", {}).get("additionalContext", "")
check("recipient: injected mid-turn", "STOP, the plan changed" in ctx and 'teammate_id="team-lead"' in ctx, str(out))
rc, out = tool()
check("recipient: injected only once", out is None)
rc, out = tool(agent="a0123456789abcdef")
check("subagent in the recipient's process: nothing injected", out is None)

rc, out = ups(block("team-lead", "STOP, the plan changed"))
check("UserPromptSubmit: not handled (idle deliveries never reach it)", rc == 0 and out is None, str(out))
rc, out = tool(sid="rx-new-session")
check("a new session of the same name gets the recent message once", out and "STOP" in json.dumps(out))

# a message that reached the recipient the normal way before any tool call: not re-injected
send("tx@t1", "rx", "already here")
transcript.write_text(json.dumps({"type": "user", "message": {"role": "user", "content": block("tx", "already here")}}) + "\n")
rc, out = tool()
check("already delivered normally: not injected", out is None, str(out))

rc, out = run("not json")
check("malformed input: exit 0, silent", rc == 0 and out is None)
print("ALL OK")
