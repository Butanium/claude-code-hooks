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
first word not sleep. NOTE 2.1.270 replaced that analyzer with a first-word
blocklist that holds only `sleep`, so on >=2.1.270 KILL_CLASS_RE is far more
conservative than the CLI is — it denies-with-advice for heredoc/redirect
shapes the CLI would now happily background. Safe (a false positive only costs
an advisory deny) but worth re-probing before trusting the deny branch.
KILL_CLASS_RE + is_kill_class approximate the
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
detects that patch in the claude binary Claude is running, via the shared
detector in `utils/_clipatch.py` (locating, module-graph parsing and caching
live there). It is consulted only once a command is already kill-class, so the
common path never opens the binary.

`auto_background_state()` is the tri-state underneath, and the third state is
the point: UNKNOWN means `canAutoBackground:` is gone entirely — upstream moved
the code out from under both the patch and this check — which is NOT the answer
"the binary is unpatched", even though both leave the deny branch armed. The
previous yes/no version could not say that, and read every patched binary as
unpatched for a month after 2.1.270 deleted the literal it anchored on.
`tests/test_clipatch.py` asserts the live binary never reads UNKNOWN; run it
after a claude update.

`CLAUDE_HOOKS_CLAUDE_BIN` (or the older `FORCE_BACKGROUND_BASH_CLAUDE_BIN`)
points the check at a specific binary — tests point it at a missing file to get
stock rules.

Teammates are distinguished from subagents by agent_id format: teammate IDs
look like ``name@team_name``, subagent IDs are bare hex. The main agent has
no agent_id at all. (In tmux/pane teammate mode agent_id is also absent —
those fall through to main-agent rules, which is the intent.)
"""
import json
import re
import sys

from utils._clipatch import PATCHED, STOCK, UNKNOWN, inspect_cached, module_runs_from_source

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
# Stock Bash tool site: `X=!cn&&pred(cmd),…,T=helper({…,canAutoBackground:X})`
# (2.1.271+: `X=!cn&&cap===void 0&&pred(cmd),` — the extra clause is the
# caller's wall-clock cap, kept by the patch); the patch turns `pred(cmd)` into
# `!0` (+ a same-length comment). Anchored on the `canAutoBackground:` property
# name, which survives identifier renames — the previous anchor was the
# `&&!/git/i.test(` literal beside it, and 2.1.270 deleted that test, which
# silently made every patched binary read as unpatched.
# Kept in sync with `patches/auto-background.py` in the patches repo.
# `[\w$]` throughout: minified names may contain `$`.
_FLAG = b"canAutoBackground:"
_FLAG_RE = re.compile(re.escape(_FLAG) + rb"([A-Za-z_$][\w$]*)[,}]")
_PATCHED_TAIL = rb"=![\w$]+&&(?:[\w$]+===void 0&&)?!0(?:/\*[a-z]*\*/| *),"
_FLAG_WINDOW = 400
def _auto_background_state(data):
    """PATCHED / STOCK / UNKNOWN for the auto-background patch.

    UNKNOWN means `canAutoBackground:` is gone entirely, i.e. upstream moved the
    code out from under both the patch and this check — which is NOT the same
    answer as "the binary is unpatched", even though both leave the deny branch
    armed.
    """
    seen = False
    for m in _FLAG_RE.finditer(data):
        seen = True
        assign = re.compile(rb"(?<![\w$])" + re.escape(m.group(1)) + _PATCHED_TAIL)
        a = assign.search(data, max(0, m.start() - _FLAG_WINDOW), m.start())
        if a:
            return PATCHED if module_runs_from_source(data, a.start()) else STOCK
    return STOCK if seen else UNKNOWN


def auto_background_state():
    return inspect_cached("auto_background", _auto_background_state) or UNKNOWN


def binary_backgrounds_everything():
    """True iff the claude binary in use carries the auto-background patch and
    the patched module actually runs from source."""
    return auto_background_state() == PATCHED


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
        "requires it, arm `bgwatch` on it (the launch hook prints the exact Monitor call)."
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
