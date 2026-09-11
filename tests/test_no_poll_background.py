#!/usr/bin/env python3
"""Regression tests for no_poll_background.py.

Each case builds a transcript, feeds the hook a PreToolUse payload on stdin and
asserts allow (empty stdout) vs deny vs force-stop.

Run: python3 tests/test_no_poll_background.py
"""

import json
import subprocess
import sys
import tempfile
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "no_poll_background.py"

TASK = "b3m9t3uk5"
OUTPUT = f"/tmp/claude-1000/-proj/abc/tasks/{TASK}.output"


def check(label, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        sys.exit(1)


# --- transcript record builders ------------------------------------------


def agent_launch(task=TASK):
    """The tool_result the agent gets back when IT backgrounds a command."""
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "toolu_1",
                    "content": (
                        f"Command running in background with ID: {task}. Output is "
                        f"being written to: /tmp/claude-1000/-proj/abc/tasks/{task}.output"
                    ),
                }
            ],
        },
    }


def bang_launch(task=TASK):
    """What a user's `!`-prefixed command looks like once the harness
    backgrounds it at the sync timeout."""
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": (
                "<bash-input>claude setup-token</bash-input>\n"
                f"<bash-stdout>Command did not complete within its 10s timeout and was "
                f"moved to the background (ID: {task}). Output is being written to: "
                f"/tmp/claude-1000/-proj/abc/tasks/{task}.output</bash-stdout>"
            ),
        },
    }


def notification(task=TASK):
    return {
        "type": "user",
        "message": {
            "role": "user",
            "content": (
                f"<task-notification><task-id>{task}</task-id><status>completed</status>"
                "</task-notification>"
            ),
        },
    }


def assistant(text="", stop_reason=None):
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": text}],
            "stop_reason": stop_reason,
        },
    }


def current_read_record():
    """The record for the call under evaluation (last line of the transcript)."""
    return assistant(f"reading {OUTPUT}")


def run(records, tool_name="Read", tool_input=None):
    if tool_input is None:
        tool_input = {"file_path": OUTPUT}
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")
        path = f.name
    payload = {"tool_name": tool_name, "tool_input": tool_input, "transcript_path": path}
    proc = subprocess.run(
        [sys.executable, str(HOOK)], input=json.dumps(payload), capture_output=True, text=True
    )
    check(f"hook exits 0 ({tool_name})", proc.returncode == 0, proc.stderr)
    return json.loads(proc.stdout) if proc.stdout.strip() else None


def decision(out):
    if out is None:
        return "allow"
    if out.get("continue") is False:
        return "force-stop"
    return out.get("hookSpecificOutput", {}).get("permissionDecision", "?")


# --- cases ---------------------------------------------------------------

# The bug this file was written for: a `!` command the harness backgrounded is
# not the agent's poll loop, and if it is parked on an interactive prompt the
# completion notification the denial promises never arrives.
check(
    "user `!` launch is not the agent's doom loop",
    decision(run([bang_launch(), assistant("on it"), current_read_record()])) == "allow",
)

check(
    "user `!` launch stays allowed on a re-read",
    decision(
        run(
            [
                bang_launch(),
                assistant("checking"),
                {"type": "user", "message": {"role": "user", "content": "any luck?"}},
                current_read_record(),
            ]
        )
    )
    == "allow",
)

# The agent's OWN earlier read of a user-launched task mentions the id too, in
# records that are not `!` records. Ownership has to be decided by the earliest
# mention, or that read silently re-attributes the task to the agent.
check(
    "agent's earlier read doesn't re-attribute a user `!` task to the agent",
    decision(
        run(
            [
                bang_launch(),
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Read",
                                "input": {"file_path": OUTPUT},
                            }
                        ],
                    },
                },
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {"type": "tool_result", "tool_use_id": "t", "content": OUTPUT}
                        ],
                    },
                },
                current_read_record(),
            ]
        )
    )
    == "allow",
)

# Everything the guard was built for still fires.
check(
    "agent launch + no notification + no yield = deny",
    decision(run([agent_launch(), assistant("polling"), current_read_record()])) == "deny",
)

check(
    "agent launch + completion notification = allow",
    decision(run([agent_launch(), notification(), current_read_record()])) == "allow",
)

check(
    "agent launch + end_turn since launch = allow",
    decision(
        run([agent_launch(), assistant("done for now", stop_reason="end_turn"), current_read_record()])
    )
    == "allow",
)

check(
    "bypass tag in the issuing assistant message = allow",
    decision(
        run([agent_launch(), assistant(f"peeking [NOT-IN-A-DOOM-READ-LOOP] at {OUTPUT}")])
    )
    == "allow",
)

check(
    "bypass tag in a Bash comment = allow",
    decision(
        run(
            [agent_launch(), assistant("peeking")],
            tool_name="Bash",
            tool_input={"command": f"cat {OUTPUT}  # [NOT-IN-A-DOOM-READ-LOOP]"},
        )
    )
    == "allow",
)

check(
    "Bash reader on an agent-launched live task = deny",
    decision(
        run(
            [agent_launch(), assistant("polling")],
            tool_name="Bash",
            tool_input={"command": f"cat {OUTPUT}"},
        )
    )
    == "deny",
)

check(
    "wait-then-read is not a poll",
    decision(
        run(
            [agent_launch(), assistant("waiting")],
            tool_name="Bash",
            tool_input={"command": f"sleep 30; cat {OUTPUT}"},
        )
    )
    == "allow",
)

check(
    "unrelated file is none of the guard's business",
    decision(run([agent_launch(), assistant("reading")], tool_input={"file_path": "/etc/hosts"}))
    == "allow",
)

check(
    "task from a prior/compacted session (no launch record) = allow",
    decision(run([assistant("reading a task I never launched here"), current_read_record()]))
    == "allow",
)

# The escalation only counts denials for THIS task, and a `!` task never gets
# there in the first place.
check(
    "re-poll after a deny, still no yield = force-stop",
    decision(
        run(
            [
                agent_launch(),
                assistant("polling"),
                {
                    "type": "user",
                    "message": {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_2",
                                "content": (
                                    f"Task {TASK} hasn't sent its completion "
                                    "<task-notification> yet AND you haven't yielded"
                                ),
                            }
                        ],
                    },
                },
                current_read_record(),
            ]
        )
    )
    == "force-stop",
)

print("\nall good")
