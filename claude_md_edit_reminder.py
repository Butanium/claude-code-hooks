#!/usr/bin/env python3
"""PreToolUse hook: when the global ~/.claude/CLAUDE.md is Read, inject a
reminder that the file is generated and edits belong in CLAUDE.template.md.

Non-blocking — the Read proceeds normally; the reminder rides along as
additionalContext. The on-disk HTML header serves humans opening the file,
but the harness strips HTML comments when injecting claudeMd, so a model
acting from context never sees it — this hook covers the Read-tool path.
"""
import json
import os
import sys
from pathlib import Path

data = json.load(sys.stdin)

if data.get("tool_name") != "Read":
    sys.exit(0)

file_path = data.get("tool_input", {}).get("file_path", "")
if not file_path:
    sys.exit(0)

config_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude")).expanduser()
global_claude_md = (config_dir / "CLAUDE.md").resolve()

try:
    target = Path(file_path).expanduser().resolve()
except OSError:
    sys.exit(0)

if target != global_claude_md:
    sys.exit(0)

print(
    json.dumps(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": (
                    f"Reminder: {global_claude_md} is AUTO-GENERATED at every session start "
                    "by hooks/detect_env.py from CLAUDE.template.md (+ env-configs/<env>.md "
                    "for the {{ENV_CONFIG}} block, + standalone files pulled in via "
                    "{{INCLUDE:path}} directives). Any direct edit will be silently wiped "
                    "at the next SessionStart. If you intend to change these instructions, "
                    "edit CLAUDE.template.md, the relevant env-configs/*.md, or the "
                    "{{INCLUDE}}'d file instead."
                ),
            }
        }
    )
)
