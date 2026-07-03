#!/usr/bin/env python3
"""PreToolUse hook: force-stops on dangerous commands."""
import json
import os
import re
import sys

data = json.load(sys.stdin)

if data.get("tool_name") != "Bash":
    sys.exit(0)

cmd = data.get("tool_input", {}).get("command", "")

# Expand ~ and $HOME for pattern matching
home = os.path.expanduser("~")
cmd_expanded = cmd.replace("~", home).replace("$HOME", home)

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

for pattern, description in DANGEROUS_PATTERNS:
    if re.search(pattern, cmd, re.IGNORECASE) or re.search(
        pattern, cmd_expanded, re.IGNORECASE
    ):
        print(
            json.dumps(
                {
                    "continue": False,
                    "stopReason": f"🛑 SECURITY STOP: Dangerous command detected ({description}): {cmd[:100]}",
                }
            )
        )
        sys.exit(0)

# Not dangerous, allow (other hooks like force_background will still run)
sys.exit(0)
