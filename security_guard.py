#!/usr/bin/env python3
"""PreToolUse hook: denies dangerous Bash commands and stops the turn.

Also pings the human's ntfy hotline, because the two in-harness signals both
depend on someone reading the session: the deny reason goes to Claude and the
stopReason goes to a terminal that may be unattended. An agent reaching for
mkfs is exactly the moment the human wants an out-of-band tap on the shoulder.
"""
import json
import os
import re
import socket
import sys
import urllib.request

# Expand ~ and $HOME for pattern matching
home = os.path.expanduser("~")

HOTLINE_ENV = "CLAUDE_HOTLINE_NTFY_TOPIC"
LEGACY_HOTLINE_ENV = "CLAUDE_HOTLINE_TOPIC"
NTFY_BASE = os.environ.get("NTFY_BASE_URL", "https://ntfy.sh").rstrip("/")
# ntfy's default per-message cap is 4 KiB; leave room for the surrounding fields.
MAX_CMD_CHARS = 2000

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
    # chmod/chown 777 or recursive on critical paths
    (r"chmod\s+(-[^\s]*\s+)*777\s+(/|~|\$HOME)", "chmod 777 on root or home"),
    (
        r"chown\s+(-[^\s]*\s+)*-[^\s]*R[^\s]*\s+[^\s]+\s+(/|~|\$HOME)",
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
    # Curl/wget piped to shell with suspicious URLs
    (r"(curl|wget).*\|\s*(ba)?sh", "piping remote script to shell"),
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

def match_danger(cmd):
    """Return the description of the first pattern `cmd` trips, or None."""
    cmd_expanded = cmd.replace("~", home).replace("$HOME", home)
    for pattern, description in DANGEROUS_PATTERNS:
        if re.search(pattern, cmd, re.IGNORECASE) or re.search(
            pattern, cmd_expanded, re.IGNORECASE
        ):
            return description
    return None


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


def notify_hotline(description, cmd, data):
    """Best-effort out-of-band ping. Never let this stop the deny from landing."""
    topic = (
        os.environ.get(HOTLINE_ENV, "").strip()
        or os.environ.get(LEGACY_HOTLINE_ENV, "").strip()
    )
    if not topic:
        print(
            f"{HOTLINE_ENV} unset — no out-of-band alert sent (see detect_env.py)",
            file=sys.stderr,
        )
        return False
    title, body = build_alert(description, cmd, data)
    req = urllib.request.Request(
        f"{NTFY_BASE}/{topic}",
        data=body.encode("utf-8"),
        headers={
            # ntfy header values must be latin-1-safe for urllib, so the emoji
            # lives in Tags (rendered by ntfy) rather than in Title.
            "Title": title.encode("ascii", "replace").decode("ascii"),
            "Priority": "high",
            "Tags": "rotating_light",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return 200 <= resp.status < 300
    except Exception as exc:  # network is a boundary; the deny matters more
        print(f"hotline ntfy failed ({exc!r}) — deny still applied", file=sys.stderr)
        return False


def main():
    data = json.load(sys.stdin)
    if data.get("tool_name") != "Bash":
        return

    cmd = data.get("tool_input", {}).get("command", "")
    description = match_danger(cmd)
    if description is None:
        # Not dangerous, allow (other hooks like force_background still run)
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
