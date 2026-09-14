#!/usr/bin/env python3
"""PreToolUse hook: two-stage guard for dangerous Bash commands.

Stage 1 is a regex table. DANGEROUS_PATTERNS (box destruction: rm -rf on root or
home, mkfs, dd onto a disk, ...) deny outright, stop the turn and ping the human's
ntfy hotline -- no model in that loop. JUDGED_PATTERNS (a download piped into a
shell) are made of everyday tokens and historically fired mostly on data (a grep
argument, a heredoc body, prose), so a match goes to stage 2 instead: the script
is fetched to disk and a restricted nested `claude -p`, running the agent in
agents/remote-script-judge.md with WebSearch as its only tool, decides. Its verdict
maps to allow-through (plus a context line for the agent), or to `ask` with a
hotline ping when it has a concern -- `deny` in permission modes where nobody
answers prompts -- and never to a turn stop. The judge's optional note reaches the
human over ntfy either way.

Hotline pings exist because the two in-harness signals both depend on someone
reading the session: the deny reason goes to Claude and the stopReason goes to a
terminal that may be unattended. An agent reaching for mkfs is exactly the moment
the human wants an out-of-band tap on the shoulder.
"""
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

# Expand ~ and $HOME for pattern matching
home = os.path.expanduser("~")

HOTLINE_ENV = "CLAUDE_HOTLINE_NTFY_TOPIC"
LEGACY_HOTLINE_ENV = "CLAUDE_HOTLINE_TOPIC"
NOTES_ENV = "CLAUDE_NTFY_TOPIC"  # judge notes on an allowed command go here, not to the hotline
NTFY_BASE = os.environ.get("NTFY_BASE_URL", "https://ntfy.sh").rstrip("/")
# ntfy's default per-message cap is 4 KiB; leave room for the surrounding fields.
MAX_CMD_CHARS = 2000

AGENT_MD = Path(__file__).resolve().parent / "agents" / "remote-script-judge.md"
GUARD_DIR = Path(os.environ.get("CLAUDE_GUARD_DIR") or tempfile.gettempdir()) / "claude-guard"
JUDGE_CMD_ENV = "CLAUDE_GUARD_JUDGE_CMD"  # tests: an executable that stands in for `claude`
SKIP_FETCH_ENV = "CLAUDE_GUARD_SKIP_FETCH"
JUDGE_TIMEOUT = 120
JUDGE_MAX_TURNS = "8"
JUDGE_MAX_BUDGET_USD = "1"
FETCH_TIMEOUT = 20
FETCH_MAX_BYTES = 512 * 1024
# The judge process gets a scrubbed environment: auth comes from the config dir
# (subscription login), not from whatever API keys the session carries.
JUDGE_ENV_KEEP = (
    "PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "APPDATA", "LOCALAPPDATA",
    "TEMP", "TMP", "LANG", "LC_ALL", "PYTHONUTF8", "CLAUDE_CONFIG_DIR",
)
# Modes where a permission prompt has nobody to answer it.
NO_PROMPT_MODES = {"bypassPermissions", "dontAsk"}

NOTE_ROUTES = ("stored", "clement-later", "clement-now", "clement-urgent")
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "ok": {"type": "boolean"},
        "reason": {"type": "string"},
        "note": {"type": "string"},
        "note_route": {"type": "string", "enum": list(NOTE_ROUTES)},
    },
    "required": ["ok", "reason"],
}
# Every note is appended here with its route; `*_journal.md` files anywhere in the
# config repo are auto-committed by sync_config.py, so notes follow the human across
# machines and sit next to the journals he already reads.
NOTES_FILE = Path(
    os.environ.get("CLAUDE_GUARD_NOTES_FILE")
    or Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "agents" / "remote-script-judge_journal.md"
)

# Root or a home directory as a COMPLETE argument — `/`, `~`, `$HOME`, the expanded home,
# each optionally with a trailing slash, and nothing after it. Deliberately not a prefix:
# `/var/tmp/x` starts with `/` but is not root, and conflating the two is what made the
# chmod/chown patterns fire on ordinary paths.
CRITICAL = rf"(?:/|~|\$HOME|{re.escape(home)})/?(?=\s|$)"

DANGEROUS_PATTERNS = [
    # Recursive delete on critical paths
    (r"rm\s+(-[^\s]*\s+)*-[^\s]*r[^\s]*\s+(/|~|\$HOME)\s*$", "rm -r on root or home"),
    (r"rm\s+(-[^\s]*\s+)*-[^\s]*r[^\s]*\s+(/|~|\$HOME)/?\s*$", "rm -r on root or home"),
    (
        rf"rm\s+(-[^\s]*\s+)*-[^\s]*r[^\s]*\s+{re.escape(home)}\s*$",
        "rm -r on home directory",
    ),
    (
        rf"rm\s+(-[^\s]*\s+)*-[^\s]*r[^\s]*\s+{re.escape(home)}/?\s*$",
        "rm -r on home directory",
    ),
    # chmod/chown 777 or recursive on critical paths.
    # CRITICAL is the whole target, not a prefix of it: a bare `(/|~|\$HOME)` also matches
    # the leading slash of every absolute path, which denied `chmod -R 777 /var/tmp/work`
    # (a real firing, 2026-08-29) and every `chown -R user /var/lib/...`.
    (rf"chmod\s+(-[^\s]*\s+)*777\s+{CRITICAL}", "chmod 777 on root or home"),
    (
        rf"chown\s+(-[^\s]*\s+)*-[^\s]*R[^\s]*\s+[^\s]+\s+{CRITICAL}",
        "recursive chown on root or home",
    ),
    # dd writing to disk devices
    (r"dd\s+.*of=/dev/[sh]d[a-z]", "dd to disk device"),
    # mkfs on devices
    (r"mkfs", "mkfs command"),
    # Fork bombs
    (r":\(\)\s*\{\s*:\|:&\s*\}\s*;:", "fork bomb"),
    # Overwriting boot/system
    (r">\s*/dev/[sh]d[a-z]", "overwrite disk device"),
    (r">\s*/boot/", "overwrite boot"),
    # --- Windows-specific ---
    # Recursive delete on critical paths
    (r"rd\s+/s\s+[/\\]?[cC]:\\?(\s|$)", "rd /s on C: drive root"),
    (r"rmdir\s+/s\s+[/\\]?[cC]:\\?(\s|$)", "rmdir /s on C: drive root"),
    (r"del\s+/[^\s]*s[^\s]*\s+[/\\]?[cC]:\\(\s|$)", "del /s on C: drive root"),
    (r"Remove-Item\s+.*-Recurse.*[cC]:\\?(\s|$)", "Remove-Item -Recurse on C: root"),
    (r"Remove-Item\s+.*[cC]:\\?\s.*-Recurse", "Remove-Item -Recurse on C: root"),
    # Format drive
    (r"format\s+[a-zA-Z]:", "format drive"),
    # Diskpart
    (r"diskpart", "diskpart command"),
    # Registry damage
    (r"reg\s+delete\s+HKLM", "reg delete on HKLM"),
    (r"reg\s+delete\s+HKCR", "reg delete on HKCR"),
    # PowerShell download + execute
    (r"IEX\s*\(.*Net\.WebClient", "PowerShell download-and-execute"),
    (r"Invoke-Expression.*DownloadString", "PowerShell download-and-execute"),
]

# Everyday tokens (curl, a pipe, sh): 6 of the first 12 firings ever were this pattern
# and none was harmful, so a match is reviewed by the judge instead of denied.
# \b matters: without it `| shasum`, `| shuf` and `| shellcheck` all read as a shell.
JUDGED_PATTERNS = [
    (r"(curl|wget).*\|\s*(ba|z|k|da)?sh\b", "piping remote script to shell"),
]

URL_AFTER_FETCHER = re.compile(r"(?:curl|wget)\b[^|;&\n]*?(https?://[^\s'\"`|;&<>()]+)")


def _match(patterns, cmd):
    cmd_expanded = cmd.replace("~", home).replace("$HOME", home)
    for pattern, description in patterns:
        if re.search(pattern, cmd, re.IGNORECASE) or re.search(
            pattern, cmd_expanded, re.IGNORECASE
        ):
            return description
    return None


def match_danger(cmd):
    """Return the description of the first hard-deny pattern `cmd` trips, or None."""
    return _match(DANGEROUS_PATTERNS, cmd)


def match_judged(cmd):
    """Return the description of the first judge-reviewed pattern `cmd` trips, or None."""
    return _match(JUDGED_PATTERNS, cmd)


# --- ntfy ---------------------------------------------------------------------------


def build_alert(description, cmd, data):
    """Build the (title, body) ntfy payload. Pure — tests call this directly."""
    shown = cmd
    if len(cmd) > MAX_CMD_CHARS:
        shown = f"{cmd[:MAX_CMD_CHARS]}\n… truncated, {len(cmd)} chars total"
    lines = [
        f"host: {socket.gethostname()}",
        f"cwd: {data.get('cwd') or '?'}",
        f"session: {data.get('session_id') or '?'}",
        "",
        shown,
    ]
    return f"SECURITY STOP: {description}", "\n".join(lines)


def ntfy_post(topic, title, body, priority="default", tags="rotating_light"):
    """Best-effort out-of-band ping. Never let this stop the decision from landing."""
    req = urllib.request.Request(
        f"{NTFY_BASE}/{topic}",
        data=body.encode("utf-8"),
        headers={
            # ntfy header values must be latin-1-safe for urllib, so the emoji
            # lives in Tags (rendered by ntfy) rather than in Title.
            "Title": title.encode("ascii", "replace").decode("ascii"),
            "Priority": priority,
            "Tags": tags,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return 200 <= resp.status < 300
    except Exception as exc:  # network is a boundary; the decision matters more
        print(f"ntfy failed for topic {topic} ({exc!r}) — decision still applied", file=sys.stderr)
        return False


def hotline_topic():
    return (
        os.environ.get(HOTLINE_ENV, "").strip()
        or os.environ.get(LEGACY_HOTLINE_ENV, "").strip()
    )


def notify_hotline(description, cmd, data, extra=""):
    topic = hotline_topic()
    if not topic:
        print(
            f"{HOTLINE_ENV} unset — no out-of-band alert sent (see detect_env.py)",
            file=sys.stderr,
        )
        return False
    title, body = build_alert(description, cmd, data)
    if extra:
        body = f"{extra}\n\n{body}"
    return ntfy_post(topic, title, body, priority="high")


def notify_note(note, route, cmd, data):
    """`clement-now` goes to the regular topic; `clement-urgent` to the hotline, high priority."""
    if route == "clement-urgent":
        topic, priority = hotline_topic(), "urgent"
    else:
        topic, priority = os.environ.get(NOTES_ENV, "").strip() or hotline_topic(), "default"
    if not topic:
        print(f"no ntfy topic for a {route} judge note — not forwarded: {note}", file=sys.stderr)
        return False
    _, body = build_alert("", cmd, data)
    return ntfy_post(topic, f"remote-script-judge note ({route})", f"{note}\n\n{body}", priority=priority, tags="speech_balloon")


def store_note(verdict, cmd, data):
    """Append the note to the journal. Never let a write failure reach the decision."""
    one_line = " ".join(cmd.split())
    entry = "\n".join([
        f"## {time.strftime('%Y-%m-%d %H:%M', time.gmtime())} UTC — {verdict['note_route']} — ok={str(verdict['ok']).lower()}",
        f"- session: {data.get('session_id') or '?'} · cwd: {data.get('cwd') or '?'} · host: {socket.gethostname()}",
        f"- command: `{one_line[:400]}`" + (" …" if len(one_line) > 400 else ""),
        f"- reason: {verdict['reason']}",
        f"- note: {verdict['note']}",
        "",
        "",
    ])
    try:
        NOTES_FILE.parent.mkdir(parents=True, exist_ok=True)
        if not NOTES_FILE.exists():
            NOTES_FILE.write_text(
                "# remote-script-judge notes\n\nAppended by `hooks/security_guard.py` whenever the judge leaves a note. "
                "Route `clement-later` means Clément should read it next time he looks into the hook; "
                "`clement-now` / `clement-urgent` were also sent over ntfy at the time.\n\n",
                encoding="utf-8",
            )
        with NOTES_FILE.open("a", encoding="utf-8") as fh:
            fh.write(entry)
        return True
    except OSError as exc:
        print(f"could not store judge note ({exc!r}): {verdict['note']}", file=sys.stderr)
        return False


# --- stage 2: fetch + judge -----------------------------------------------------------


class JudgeUnavailable(Exception):
    pass


def load_agent(path=AGENT_MD):
    """Parse the agent markdown: `---` frontmatter of `key: value` lines, then the prompt."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        raise JudgeUnavailable(f"{path} has no frontmatter")
    _, front, prompt = text.split("---", 2)
    agent = {}
    for line in front.strip().splitlines():
        key, _, value = line.partition(":")
        agent[key.strip()] = value.strip()
    for key in ("name", "description", "model", "tools"):
        if not agent.get(key):
            raise JudgeUnavailable(f"{path} frontmatter lacks `{key}`")
    agent["tools"] = [t.strip() for t in agent["tools"].split(",") if t.strip()]
    agent["prompt"] = prompt.strip()
    return agent


def extract_url(cmd):
    m = URL_AFTER_FETCHER.search(cmd)
    return m.group(1) if m else None


def fetch_script(url):
    """Download `url` to GUARD_DIR. Returns (path, text, truncated) or raises."""
    GUARD_DIR.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "curl/8.0 (claude security_guard)"})
    with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
        raw = resp.read(FETCH_MAX_BYTES + 1)
    truncated = len(raw) > FETCH_MAX_BYTES
    raw = raw[:FETCH_MAX_BYTES]
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:12]
    path = GUARD_DIR / f"{digest}-{int(time.time())}.sh"
    path.write_bytes(raw)
    return path, raw.decode("utf-8", errors="replace"), truncated


def build_user_message(cmd, url, script_text, saved_path, fetch_error, truncated):
    parts = [f"Command:\n{cmd}", ""]
    if url is None:
        parts.append("No URL was found after curl/wget in the command, so nothing was fetched.")
    elif fetch_error:
        parts.append(f"Fetching {url} failed: {fetch_error}")
    else:
        size = f"{len(script_text)} chars" + (", truncated" if truncated else "")
        parts.append(f"Fetched script from {url} ({size}, saved at {saved_path}):\n\n{script_text}")
    return "\n".join(parts)


def build_judge_argv(agent, user_msg, claude_exe="claude"):
    """The nested-claude launch. Pure — tests assert on it.

    Verified shape (2.1.257): --restricted drops settings files (so no hooks, no
    CLAUDE.md, no MCP with --strict-mcp-config); --tools plus --allowedTools names the
    judge's only tool AND grants it, since without the grant the call is refused and
    the judge cannot see why; the agent's own tool list must carry StructuredOutput or
    --json-schema is silently not enforced.
    """
    cli_tools = [t for t in agent["tools"] if t != "StructuredOutput"]
    agents_json = json.dumps({
        agent["name"]: {
            "description": agent["description"],
            "prompt": agent["prompt"],
            "tools": agent["tools"],
            "model": agent["model"],
        }
    })
    return [
        claude_exe, "-p",
        "--model", agent["model"],
        "--restricted", "--strict-mcp-config",
        "--tools", ",".join(cli_tools),
        "--allowedTools", ",".join(cli_tools),
        "--disable-slash-commands",
        "--agents", agents_json,
        "--agent", agent["name"],
        "--max-turns", JUDGE_MAX_TURNS,
        "--max-budget-usd", JUDGE_MAX_BUDGET_USD,
        "--json-schema", json.dumps(VERDICT_SCHEMA),
        "--output-format", "json",
        user_msg,
    ]


def judge_executable():
    stub = os.environ.get(JUDGE_CMD_ENV, "").strip()
    if stub:
        return [sys.executable, stub] if stub.endswith(".py") else [stub]
    exe = shutil.which("claude")
    if not exe:
        raise JudgeUnavailable("`claude` not on PATH")
    return [exe]


def run_judge(user_msg, agent):
    """Run the judge and return its verdict dict. Raises JudgeUnavailable on any failure."""
    exe = judge_executable()
    argv = build_judge_argv(agent, user_msg)
    argv = exe + argv[1:]
    env = {k: v for k, v in os.environ.items() if k in JUDGE_ENV_KEEP or k.startswith("CLAUDE_GUARD_")}
    GUARD_DIR.mkdir(parents=True, exist_ok=True)
    try:
        res = subprocess.run(
            argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=JUDGE_TIMEOUT, env=env, cwd=GUARD_DIR,
        )
    except subprocess.TimeoutExpired:
        raise JudgeUnavailable(f"judge timed out after {JUDGE_TIMEOUT}s")
    except OSError as exc:
        raise JudgeUnavailable(f"could not launch judge: {exc}")
    if res.returncode != 0:
        raise JudgeUnavailable(f"judge exited {res.returncode}: {res.stderr.strip()[-500:]}")
    try:
        out = json.loads(res.stdout)
    except ValueError:
        raise JudgeUnavailable(f"judge output was not JSON: {res.stdout.strip()[:300]}")
    verdict = out.get("structured_output") if isinstance(out, dict) else None
    if not isinstance(verdict, dict) or not isinstance(verdict.get("ok"), bool) or not isinstance(verdict.get("reason"), str):
        raise JudgeUnavailable(f"judge returned no schema-conformant verdict: {str(out)[:300]}")
    verdict["note"] = (verdict.get("note") or "").strip()
    route = verdict.get("note_route")
    verdict["note_route"] = route if route in NOTE_ROUTES else "stored"
    return verdict


def decide_judged(verdict, error, permission_mode, saved_path):
    """Map a judge verdict (or its absence) to the hook's output dict. Pure."""
    saved = f" Saved copy: {saved_path}" if saved_path else ""
    if verdict is not None and verdict["ok"]:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": (
                    f"security_guard: this command matched the remote-script pattern and was "
                    f"reviewed by remote-script-judge, which allowed it: {verdict['reason']}{saved}"
                ),
            }
        }
    if verdict is None:
        reason = f"remote-script-judge unavailable ({error}); review the script yourself before running it.{saved}"
    else:
        reason = f"remote-script-judge has a concern: {verdict['reason']}{saved}"
    decision = "deny" if permission_mode in NO_PROMPT_MODES else "ask"
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": decision,
            "permissionDecisionReason": f"🛑 SECURITY: {reason}",
        }
    }


def handle_judged(description, cmd, data):
    url = extract_url(cmd)
    saved_path = script_text = fetch_error = None
    truncated = False
    if url and os.environ.get(SKIP_FETCH_ENV):
        fetch_error = f"fetch skipped ({SKIP_FETCH_ENV} is set)"
    elif url:
        try:
            saved_path, script_text, truncated = fetch_script(url)
        except Exception as exc:
            fetch_error = f"{type(exc).__name__}: {exc}"
    verdict = error = None
    try:
        agent = load_agent()
        user_msg = build_user_message(cmd, url, script_text, saved_path, fetch_error, truncated)
        verdict = run_judge(user_msg, agent)
    except JudgeUnavailable as exc:
        error = str(exc)
        print(f"security_guard: {error}", file=sys.stderr)

    out = decide_judged(verdict, error, data.get("permission_mode"), saved_path)
    note = verdict["note"] if verdict else ""
    if note:
        store_note(verdict, cmd, data)
    if "permissionDecision" in out["hookSpecificOutput"]:
        # The human is being paged anyway; the note rides along whatever its route.
        extra = out["hookSpecificOutput"]["permissionDecisionReason"]
        if note:
            extra += f"\n\njudge note ({verdict['note_route']}): {note}"
        notify_hotline(description, cmd, data, extra=extra)
    elif note and verdict["note_route"] in ("clement-now", "clement-urgent"):
        notify_note(note, verdict["note_route"], cmd, data)
    print(json.dumps(out))


def main():
    data = json.load(sys.stdin)
    if data.get("tool_name") != "Bash":
        return

    cmd = data.get("tool_input", {}).get("command", "")
    description = match_danger(cmd)
    if description is None:
        judged = match_judged(cmd)
        if judged is not None:
            handle_judged(judged, cmd, data)
        # Otherwise not dangerous, allow (other hooks like force_background still run)
        return

    notify_hotline(description, cmd, data)
    reason = f"🛑 SECURITY STOP: Dangerous command detected ({description}): {cmd[:100]}"
    print(
        json.dumps(
            {
                # `continue: false` alone only halts the agent loop *after* the
                # tool runs -- blocking the call needs permissionDecision: deny.
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                },
                "continue": False,
                "stopReason": reason,
            }
        )
    )


if __name__ == "__main__":
    main()
    sys.exit(0)
