#!/usr/bin/env python3
"""PreToolUse hook for Bash sync timeout policy.

- Main agent / teammates, requested sync timeout > 60s:
  - Command auto-backgroundable at timeout: clamp the timeout to 30s. The
    CLI moves such commands to background when the sync timeout expires (no
    kill), so the clamp only caps how long the conversation blocks. Goal: a
    long sync wait burns turns for no reason — if you expect >60s, you should
    have used run_in_background=true upfront.
  - Command NOT auto-backgroundable: deny with advice. For these the sync
    timeout is a hard SIGTERM kill (exit 143), so clamping would kill work
    the model sized its timeout to protect (this bit us: a 240s request
    clamped to 30s killed a pipeline mid-run with partial output).
- Subagents: block `run_in_background=true` unless command contains
  BACKGROUND_NEEDED escape hatch (e.g. starting a server). High sync timeouts
  remain allowed because subagents can't usefully background — they don't
  receive completion notifications.

Auto-backgroundability (CLI v2.1.216, re-probed on v2.1.250; undocumented,
details in https://github.com/anthropics/claude-code/issues/79879): the CLI's
static shell analyzer must fully decompose the command, no git subcommand,
first word not sleep. KILL_CLASS_RE + is_kill_class approximate the
analyzer's rejections we verified empirically: $VAR/backtick redirect
targets, process substitution, and heredocs *except* the one shape that
decomposes — quoted delimiter with every redirect placed before the operator
(`cmd > out.log 2>&1 <<'EOF'`; body content is irrelevant). An unquoted
`<<EOF` kills even without a redirect, and `<<'EOF' > out.log` (redirect
after the operator) kills too. A false positive here just means
deny-with-advice instead of clamp, which is safe; a false negative means the
old behavior (clamp, kill at 30s), no worse than before this check existed.
Probes: tests/test_force_background_bash.py, ENGINEERING_LOGS.md 2026-08-28.

Patched binaries: the `auto-background.py` patch from
https://github.com/Butanium/claude-code-patches makes EVERY command
backgroundable at timeout, so the kill class no longer exists there and the
deny branch would only get in the way. `binary_backgrounds_everything()`
detects that patch in the claude binary Claude is running — the patched code
shape must be present AND that module must run from source rather than from
its stale bytecode (see the patch repo's `zz-bytecode-off.py`; a patched text
whose bytecode is still enabled is inert). The check is cached per
(path, size, mtime) in the temp dir; `FORCE_BACKGROUND_BASH_CLAUDE_BIN` points
it at a specific binary (tests point it at a missing file to get stock rules).

Teammates are distinguished from subagents by agent_id format: teammate IDs
look like ``name@team_name``, subagent IDs are bare hex. The main agent has
no agent_id at all. (In tmux/pane teammate mode agent_id is also absent —
those fall through to main-agent rules, which is the intent.)
"""
import json
import os
import re
import shutil
import struct
import sys
import tempfile

KILL_CLASS_RE = re.compile(
    r"<<<"  # herestring (untested; conservative)
    r"|[<>]\("  # process substitution
    r"|[<>]\|?\s*[\"']?[$`]"  # $VAR or `...` as a redirect target (verified kill)
)
# `<<'EOF'` / `<<EOF` / `<<-EOF`: quote char (if any), delimiter, rest of that line
HEREDOC_OP = re.compile(r"<<-?\s*(['\"]?)(\w+)\1([^\n]*)")
GIT_RE = re.compile(r"(?:^|[;&|(]|\$\(|`)\s*(?:command\s+|builtin\s+)?git\b")
SLEEP_RE = re.compile(r"^\s*sleep\b")

# --- detection of the auto-background CLI patch -------------------------------
# Stock Bash tool site: `X=!cn&&pred(cmd),Y=!cn&&!/git/i.test(cmd)`; the patch
# turns `pred(cmd)` into `!0` (+ a same-length comment). The `/git/i.test(`
# literal is the stable anchor (it also occurs at the PowerShell site, whose
# predicate is `await …` and so never matches the stock/patched shapes).
_GIT_TEST = b"&&!/git/i.test("
_PATCHED_RE = re.compile(rb"=!(\w+)&&!0(?:/\*[a-z]*\*/| *),(\w+)=!\1$")
_TRAILER = b"\n---- Bun! ----\n"
_REC = 52


def _bun_module_bytecode_len(data, off):
    """Length of the JSC bytecode blob for the Bun standalone module whose JS
    text covers file offset `off` (0 = that module runs from source). None if the
    graph can't be parsed. Mirrors `_bungraph.py` in the patches repo."""
    t = data.rfind(_TRAILER)
    if t == -1:
        return None
    mp_off, mp_len = struct.unpack_from("<II", data, t - 24)
    if mp_len == 0 or mp_len % _REC:
        return None
    p = (t // 512) * 512
    base = None
    while p >= 0:
        (count,) = struct.unpack_from("<Q", data, p)
        if 0 <= (t + len(_TRAILER)) - (p + 8) - count < 65536:
            tbl = p + 8 + mp_off
            if tbl + mp_len <= len(data):
                noff, nlen = struct.unpack_from("<II", data, tbl)
                if 0 < nlen < 512 and data[p + 8 + noff : p + 8 + noff + 8] == b"/$bunfs/":
                    base = p + 8
                    break
        p -= 512
    if base is None:
        return None
    tbl = base + mp_off
    for i in range(mp_len // _REC):
        r = tbl + i * _REC
        _n, _nl, coff, clen, _s, _sl, _b, blen = struct.unpack_from("<8I", data, r)
        if base + coff <= off < base + coff + clen:
            return blen
    return None


def _inspect_binary(path):
    with open(path, "rb") as f:
        data = f.read()
    pos = 0
    while (i := data.find(_GIT_TEST, pos)) != -1:
        if _PATCHED_RE.search(data, max(0, i - 80), i):
            return _bun_module_bytecode_len(data, i) == 0
        pos = i + 1
    return False


def binary_backgrounds_everything():
    """True iff the claude binary in use carries the auto-background patch and
    the patched module actually runs from source."""
    path = os.environ.get("FORCE_BACKGROUND_BASH_CLAUDE_BIN") or shutil.which("claude")
    if not path:
        return False
    try:
        path = os.path.realpath(path)
        st = os.stat(path)
    except OSError:
        return False
    key = [path, st.st_size, int(st.st_mtime)]
    cache = os.path.join(tempfile.gettempdir(), f"force_background_bash_patch_{os.getuid()}.json")
    try:
        with open(cache) as f:
            c = json.load(f)
        if c.get("key") == key:
            return bool(c.get("patched"))
    except (OSError, ValueError):
        pass
    try:
        patched = _inspect_binary(path)
    except OSError:
        return False
    try:
        tmp = cache + f".{os.getpid()}"
        with open(tmp, "w") as f:
            json.dump({"key": key, "patched": patched}, f)
        os.replace(tmp, cache)
    except OSError:
        pass
    return patched


def heredoc_kills(command: str) -> bool:
    """A heredoc survives only with a quoted delimiter and no redirect after the
    operator on its line (`cmd > out 2>&1 <<'EOF'`, verified on 2.1.250)."""
    for m in HEREDOC_OP.finditer(command):
        quoted, rest = m.group(1), m.group(3)
        if not quoted or re.search(r"[<>]", rest):
            return True
    return False


def is_kill_class(command: str) -> bool:
    """True if the CLI would SIGTERM-kill this command at sync timeout
    instead of moving it to background (approximation, see module docstring)."""
    return bool(
        KILL_CLASS_RE.search(command)
        or heredoc_kills(command)
        or GIT_RE.search(command)
        or SLEEP_RE.match(command)
    )


def main():
    data = json.load(sys.stdin)

    if data.get("tool_name") != "Bash":
        sys.exit(0)

    tool_input = data.get("tool_input", {})
    cmd = tool_input.get("command", "")
    agent_id = data.get("agent_id", "")
    is_subagent = bool(agent_id) and "@" not in agent_id

    # --- Subagent: block run_in_background unless escape hatch ---
    if is_subagent and tool_input.get("run_in_background"):
        if "BACKGROUND_NEEDED" in cmd:
            sys.exit(0)
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": (
                            "BLOCKED: Subagents cannot use run_in_background=true. "
                            "Unlike the main agent, subagents do not receive background "
                            "task completion notifications — this leads to doom loops "
                            "where you poll repeatedly wasting turns. Instead, run the "
                            "command synchronously with a high timeout (e.g. "
                            "timeout=600000 for 10min). If you genuinely need background "
                            "execution (e.g. starting a server), include "
                            "BACKGROUND_NEEDED in your command: "
                            "echo BACKGROUND_NEEDED && your_actual_command"
                        ),
                    }
                }
            )
        )
        sys.exit(0)

    # --- Subagent without background: high sync timeouts allowed (only mode they have) ---
    if is_subagent:
        sys.exit(0)

    # --- Main agent / teammate: already background, leave alone ---
    if tool_input.get("run_in_background"):
        sys.exit(0)

    # --- Main agent / teammate: sync timeout policy for > 60s requests ---
    timeout = tool_input.get("timeout", 10000)
    if timeout <= 60000:
        sys.exit(0)

    original_timeout_s = int(timeout / 1000)

    if is_kill_class(cmd) and not binary_backgrounds_everything():
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": (
                            f"BLOCKED: you requested a {original_timeout_s}s sync "
                            "timeout for a command Claude Code cannot move to "
                            "background at timeout (it contains a $VAR/backtick "
                            "redirect target, git, leading sleep, an unquoted "
                            "heredoc, or a heredoc with a redirect after the "
                            "<<'EOF' operator — for these, sync timeout is a hard "
                            "SIGTERM kill, see "
                            "https://github.com/anthropics/claude-code/issues/79879). "
                            "A long sync wait here risks losing the work at the "
                            "timeout boundary. Re-run with run_in_background=true "
                            "and monitor it (Monitor / task notification), or "
                            "restructure the command so it can be auto-backgrounded: "
                            "literal redirect paths, and heredocs written as "
                            "`cmd > out.log 2>&1 <<'EOF'` (quoted delimiter, "
                            "redirects before the operator)."
                        ),
                    }
                }
            )
        )
        sys.exit(0)

    tool_input["timeout"] = 30000
    message = (
        f"Sync timeout policy: you requested a {original_timeout_s}s sync timeout "
        "(>60s); clamped to 30s. This command passed the auto-background check, so "
        "when the 30s sync timeout expires the Bash tool moves it to background "
        "(it doesn't kill it) — the clamp only caps how long the conversation "
        "blocks. If you expect this to take >60s, prefer run_in_background=true "
        "upfront to skip the sync wait entirely, which can allow you to work on "
        "other stuff while it's running and have stronger monitors: if this task "
        "requires it, don't forget to monitor it properly with Monitor or /loop."
    )
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "updatedInput": tool_input,
                    "additionalContext": message,
                }
            }
        )
    )


if __name__ == "__main__":
    main()
