#!/usr/bin/env python3
"""PreToolUse hook for Bash sync timeout policy.

- Main agent / teammates, requested sync timeout > 60s:
  - Command auto-backgroundable at timeout: clamp the timeout to 60s, silently.
    The CLI moves such commands to background when the sync timeout expires (no
    kill), so the clamp only caps how long the conversation blocks. Goal: a
    long sync wait burns turns for no reason — if you expect >60s, you should
    have used run_in_background=true upfront. (Until 2026-09-25 this clamped to
    30s with a paragraph of notice, so asking for 90s bought less than asking
    for 60s; the notice was noise on 92% of firings.)
  - Command NOT auto-backgroundable: deny with advice. For these the sync
    timeout is a hard SIGTERM kill (exit 143), so clamping would kill work
    the model sized its timeout to protect (this bit us: a 240s request
    clamped to 30s killed a pipeline mid-run with partial output).
- In-process teammates: block `run_in_background=true` unless the command
  contains the BACKGROUND_NEEDED escape hatch (e.g. starting a server). An idle
  in-process teammate is not woken by its own task notifications or Monitor
  events; they wait until someone messages it. High sync timeouts stay allowed.
- Subagents and forks get the main-agent rules. Until 2026-09-25 they were the
  ones blocked, on the premise that they can't wait for a notification; on
  2.1.280 a subagent that ends its turn with a task running is re-invoked by
  that task's notification (delegation audit, 2026-09-25).

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

Agent kinds: `utils/_agent_kind` (tmux teammates and the main agent carry no
agent_id; subagents and in-process teammates carry a bare one, told apart by the
CLI's subagent .meta.json).
"""
import json
import re
import sys

from force_background_sleep import is_watchdog
from utils._agent_kind import is_in_process_teammate
from utils._clipatch import PATCHED, STOCK, UNKNOWN, inspect_cached, module_runs_from_source

CLAMP_MS = 60000

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
    in_process = is_in_process_teammate(data)

    # --- In-process teammate: block run_in_background unless escape hatch ---
    if in_process and tool_input.get("run_in_background"):
        if "BACKGROUND_NEEDED" in cmd:
            sys.exit(0)
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PreToolUse",
                        "permissionDecision": "deny",
                        "permissionDecisionReason": (
                            "BLOCKED: run_in_background=true in an in-process teammate. "
                            "While you're idle, your own background-task notifications "
                            "and Monitor events don't wake you — they wait until someone "
                            "messages you — so a background job would sit finished and "
                            "unread. Run the command synchronously with a high timeout "
                            "instead (up to timeout=600000, 10 min). If you genuinely "
                            "need a process that outlives the call (e.g. a server), "
                            "include BACKGROUND_NEEDED in the command: "
                            "echo BACKGROUND_NEEDED && your_actual_command"
                        ),
                    }
                }
            )
        )
        sys.exit(0)

    # --- In-process teammate without background: high sync timeouts allowed ---
    if in_process:
        sys.exit(0)

    # --- Main agent / teammate: already background, leave alone ---
    if tool_input.get("run_in_background"):
        sys.exit(0)

    # --- A sleep watchdog: force_background_sleep.py backgrounds it. Both hooks see
    # the original input and the CLI keeps one updatedInput, so a clamp emitted here
    # replaced the sleep hook's run_in_background: `sleep 90; cat …` then blocked 60s
    # in the foreground under an "Auto-backgrounded" note (2026-09-25, 8 of 19 cases).
    if is_watchdog(cmd):
        sys.exit(0)

    # --- Main agent / teammate: sync timeout policy for > 60s requests ---
    timeout = tool_input.get("timeout", 10000)
    if timeout <= CLAMP_MS:
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

    # Silent clamp: when the command outlives 60s the CLI's own "moved to the
    # background" result says so; a notice here fired on every >60s request, and
    # 610 of 661 of those commands finished inside 30s (archive sweep, 2026-09-25).
    tool_input["timeout"] = CLAMP_MS
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "updatedInput": tool_input}}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Non-blocking error: surfaced to the user, the command still runs.
        import traceback

        traceback.print_exc()
        sys.exit(1)
