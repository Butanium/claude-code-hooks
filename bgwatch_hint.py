#!/usr/bin/env python3
"""PostToolUse(Bash) hook: when a command was moved to the background, hand the model
the exact Monitor call that watches it, so arming a watcher is a yes/no instead of
four decisions (pattern, buffering, timeout, exit condition).

Fires only when `tool_response.backgroundTaskId` is present (explicit
run_in_background, or auto-backgrounded at the sync timeout). Subagents are skipped:
they receive neither completion notifications nor Monitor events.

Which file to watch: if the command redirects stdout to a file (`> job.log 2>&1`), that
file — the first whowill A/B (2026-09-14) showed every instance re-pointing the hint at
the job's own log when the hint named the harness task file. Otherwise the harness task
output file, which isn't in the hook payload: it is
<tmp>/claude-<uid>/<cwd slug>/<session_id>/tasks/<task_id>.output, located by glob so a
slug-rule change can't silently break the hint (falls back to the computed path).
"""
import glob
import json
import os
import re
import sys
import tempfile

from utils._agent_kind import is_subagent
from utils._clipatch import PATCHED, STOCK, UNKNOWN, inspect_cached, module_runs_from_source, text_patch_state

# --- is Monitor loaded up front, or behind ToolSearch? ------------------------
# Monitor ships deferred, so stock the model must spend a `ToolSearch
# select:Monitor` call before it can arm anything — worth one line of the hint.
# The `monitor-undefer.py` patch (https://github.com/Butanium/claude-code-patches)
# flips that flag, and then the line is dead text plus a nudge toward a call the
# model does not need. These byte strings are the patch's own PATTERN and
# PATCHED shapes, so a patch that still applies cannot disagree with this
# check; `shouldDefer:!0` alone has ~46 copies in the binary, hence the
# neighbouring property names, which are structural and survive minification.
# The property after the flag moved at 2.1.271, hence one pair per shape.
_MONITOR_ANCHORS = (
    (b"maxResultSizeChars:1e4,shouldDefer:!0,permissionCheckFailureDecision",
     b"maxResultSizeChars:1e4,shouldDefer:!1,permissionCheckFailureDecision"),      # <= 2.1.257
    (b'maxResultSizeChars:1e4,shouldDefer:!0,userFacingName(){return"Monitor"}',
     b'maxResultSizeChars:1e4,shouldDefer:!1,userFacingName(){return"Monitor"}'),   # >= 2.1.271
)


def monitor_loaded_upfront() -> bool:
    """True only when we positively confirmed the undefer patch is in effect.
    UNKNOWN (anchor moved, binary unreadable) keeps the ToolSearch line, which
    is the harmless answer: at worst it restates something already true."""
    return any(
        text_patch_state(stock, patched, f"monitor_undefer_{i}") == PATCHED
        for i, (stock, patched) in enumerate(_MONITOR_ANCHORS)
    )


# --- does `persistent: true` still mean "no deadline"? -------------------------
# 2.1.271 put every Monitor on a <=30-min deadline behind the GrowthBook flag
# `tengu_breezy_crescent` and dropped `persistent` from the schema, so a call
# that still passes it is silently capped. The `monitor-persistent.py` patch
# rewrites the gate to `return!1&&<id>("tengu_breezy_crescent")}`. A binary
# with no flag string at all predates the change and honours persistent as-is.
_PERSIST_FLAG = b'"tengu_breezy_crescent"'
_PERSIST_STOCK_RE = re.compile(rb"return [A-Za-z_$][\w$]*\(" + re.escape(_PERSIST_FLAG) + rb",!0\)\}")
_PERSIST_PATCHED_RE = re.compile(rb"return!1&&[A-Za-z_$][\w$]*\(" + re.escape(_PERSIST_FLAG) + rb"\)\}")
_PRE_FLAG = "pre-flag"


def _persistent_state(data):
    """PATCHED / STOCK / _PRE_FLAG / UNKNOWN for the monitor-persistent patch."""
    m = _PERSIST_PATCHED_RE.search(data)
    if m:
        return PATCHED if module_runs_from_source(data, m.start()) else STOCK
    if _PERSIST_STOCK_RE.search(data):
        return STOCK
    if _PERSIST_FLAG not in data:
        return _PRE_FLAG
    return UNKNOWN


def monitor_persistent_available() -> bool:
    """True when `persistent: true` gives a watch with no deadline: a pre-2.1.271
    binary, or a 2.1.271+ one carrying the monitor-persistent patch. STOCK and
    UNKNOWN both get the capped form of the hint, which is the harmless answer."""
    return inspect_cached("monitor_persistent", _persistent_state) in (PATCHED, _PRE_FLAG)


def output_file(session_id: str, task_id: str, cwd: str) -> str:
    uid = os.getuid() if hasattr(os, "getuid") else ""
    base = os.path.join(tempfile.gettempdir(), f"claude-{uid}")
    hits = glob.glob(os.path.join(base, "*", session_id, "tasks", f"{task_id}.output"))
    if hits:
        return hits[0]
    slug = cwd.replace("/", "-").replace(".", "-").replace("\\", "-")
    return os.path.join(base, slug, session_id, "tasks", f"{task_id}.output")


HEREDOC_RE = re.compile(r"<<(-?)\s*(['\"]?)([A-Za-z_]\w*)\2")
QUOTED_RE = re.compile(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"")
REDIRECT_RE = re.compile(r"(?<![<>&0-9])(?:&>>?|>>?)\s*([^\s;&|()<>]+)")
ASSIGN_RE = re.compile(r"(?:^|[;&|\s])(?:export\s+)?([A-Za-z_]\w*)=([^\s;&|]+)")
LEADING_CD_RE = re.compile(r"^\s*cd\s+([^\s;&|]+)\s*(?:&&|;)")
# a redirect after one of these writes a file the job reads or a note, not the job's output
WRITER_CMDS = ("cat", "echo", "printf", "tee", "date", "true", ":")


def strip_heredocs(command: str) -> str:
    """Drop heredoc bodies: a `>` inside a script piped to `python - <<'EOF'` is code, not a redirect."""
    lines, out, end = command.split("\n"), [], None
    for line in lines:
        if end is not None:
            if line.strip() == end:
                end = None
            continue
        out.append(line)
        m = HEREDOC_RE.search(line)
        if m:
            end = m.group(3)
    return "\n".join(out)


def redirect_target(command: str, cwd: str) -> tuple[str | None, str | None]:
    """(resolved path, raw text) of the job's stdout redirect, or (None, None) when there is
    none. Heredoc bodies and quoted strings are ignored, and so are redirects of commands that
    write a file for the job (`cat > run.py <<EOF`, `echo … > cfg`). `$VAR`s set earlier in the
    command (`L=/tmp/x; … > $L`) or in the environment are expanded; if one can't be, the path
    comes back None with the raw text, since a watcher needs a path its own shell can open."""
    text = strip_heredocs(command)
    text = re.sub(r"(&?>>?)\s*(['\"])([^'\"\s]*)\2", r"\1 \3", text)  # keep quoted redirect targets
    text = QUOTED_RE.sub("''", text)
    targets = []
    for m in REDIRECT_RE.finditer(text):
        if m.group(1) == "/dev/null" or m.group(1).startswith("&"):
            continue
        simple = re.split(r"&&|\|\||[;|\n]", text[: m.start()])[-1].split()
        if simple and simple[0] in WRITER_CMDS:
            continue
        targets.append(m.group(1))
    if not targets:
        return None, None
    raw = targets[-1]
    env = dict(os.environ)
    for name, value in ASSIGN_RE.findall(text[: text.rfind(raw)]):
        env[name] = re.sub(r"\$\{?(\w+)\}?", lambda v: env.get(v.group(1), v.group(0)), value.strip("'\""))
    target = re.sub(r"\$\{?(\w+)\}?", lambda v: env.get(v.group(1), v.group(0)), raw)
    target = os.path.expanduser(target)
    if "$" in target or "`" in target:
        return None, raw
    base = cwd
    m = LEADING_CD_RE.match(text)
    if m:
        base = os.path.join(cwd, os.path.expanduser(m.group(1)))
    return os.path.normpath(os.path.join(base, target)), raw


def main() -> None:
    data = json.load(sys.stdin)
    if data.get("tool_name") != "Bash":
        return
    if is_subagent(data):  # in-process teammates get the hint; subagents can't wait on a Monitor
        return
    resp = data.get("tool_response") or {}
    task_id = resp.get("backgroundTaskId") if isinstance(resp, dict) else None
    if not task_id:
        return
    tool_input = data.get("tool_input") or {}
    command = (tool_input.get("command") or "").lstrip()
    if command.startswith("sleep ") or command.startswith("bgwatch"):
        return  # a timer or a watcher is not a job to watch
    desc = (tool_input.get("description") or "background job").replace('"', "'")
    cwd = data.get("cwd", "")
    task_file = output_file(data.get("session_id", ""), task_id, cwd)
    log, raw = redirect_target(command, cwd)
    lead = f"Background task {task_id} launched."
    if log:
        rel = os.path.relpath(log, cwd)
        watch = rel if not rel.startswith("..") else log
        alt = f"; not `bgwatch {task_id}`, whose task file only gets what the redirect doesn't catch"
    elif raw:
        watch = f"<absolute path of {raw}>"
        lead += (f" Its stdout goes to `{raw}`, which this hook can't resolve: give bgwatch that file's absolute path, "
                 f"not the task id, whose file stays empty when stdout is redirected.")
        alt = ""
    else:
        watch = task_id  # bgwatch resolves a bare task id to its output file
        alt = ""
    if monitor_persistent_available():
        lifetime, cap_note = "persistent=true", ""
    else:
        lifetime = "timeout_ms=1800000"
        cap_note = " This build caps every watch at 30 min and notifies you at expiry; re-arm it then if the job is still running."
    hint = (
        f"{lead} If it runs longer than a couple of minutes, arm its watcher now "
        f"(one call; then keep working or end the turn — do not poll):\n"
        f'  Monitor(command="bgwatch {watch}", {lifetime}, description="{desc}")\n'
        f"bgwatch wakes you for failure lines, a heartbeat that backs off from 1 to 10 min, silence longer than "
        f"the job's own output cadence, and the job's exit (detected because the job holds that file open — "
        f"no --pid/--pgrep needed when the job writes the watched file{alt}); then it exits itself. "
        f"Add --match RE for a progress marker, --ignore RE / --fail-also RE to tune patterns (`bgwatch --help`). "
        f"Not needed for a job that ends in seconds (the completion notification covers it), nor for a server or tunnel, which isn't meant to end."
        + cap_note
        + ("" if monitor_loaded_upfront() else " Monitor is a deferred tool \u2014 `ToolSearch select:Monitor` first if it isn't loaded.")
    )
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": hint}}))


if __name__ == "__main__":
    main()
