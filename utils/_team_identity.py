"""Which team member is this hook call running for? -> (team, name) or None.

- A name@team agent_id (in-process team member) says it directly.
- A pane (tmux) teammate is its own `claude` process started with
  `--agent-name NAME --team-name TEAM`; hook input carries no agent_id, so
  the hook reads the command line of its nearest `claude` ancestor process.
- The lead is found by `leadSessionId` in ~/.claude/teams/*/config.json.
- A bare agent_id (subagent, in-process teammate) -> None: the caller is a
  loop inside someone else's session, not an addressable member.
"""
import json
import os
from pathlib import Path

TEAMS = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "teams"


def _ancestor_args(max_depth: int = 8) -> list[str] | None:
    pid = os.getppid()
    for _ in range(max_depth):
        try:
            args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            args = [a.decode("utf-8", "replace") for a in args if a]
            if "--agent-name" in args and "--team-name" in args:
                return args
            stat = Path(f"/proc/{pid}/stat").read_text()
            pid = int(stat.rsplit(")", 1)[1].split()[1])  # ppid, after the parenthesised comm
        except (OSError, ValueError, IndexError):
            return None
        if pid <= 1:
            return None
    return None


def identity(data: dict) -> tuple[str, str] | None:
    agent_id = data.get("agent_id") or ""
    if "@" in agent_id:
        name, team = agent_id.split("@", 1)
        return team, name
    if agent_id:
        return None
    args = _ancestor_args()
    if args:
        return args[args.index("--team-name") + 1], args[args.index("--agent-name") + 1]
    sid = data.get("session_id")
    if sid and TEAMS.is_dir():
        for cfg in TEAMS.glob("*/config.json"):
            try:
                if json.loads(cfg.read_text()).get("leadSessionId") == sid:
                    return cfg.parent.name, "team-lead"
            except (OSError, ValueError):
                continue
    return None


def members(team: str) -> set[str]:
    try:
        cfg = json.loads((TEAMS / team / "config.json").read_text())
    except (OSError, ValueError):
        return set()
    return {m.get("name") for m in cfg.get("members", []) if m.get("name")}
