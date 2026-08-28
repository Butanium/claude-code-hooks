#!/usr/bin/env python3
"""PreToolUse hook: denies `| tail` / `| head` on background Bash commands when
the pipe would truncate the output of a command that could plausibly run long.

A background command's output lands in a file the agent reads afterwards;
`pytest ... 2>&1 | tail -20` throws away everything but the last 20 lines of
that file, stack trace included. The hook asks for the pipe to be dropped and
the file tailed/grepped later. `NEEDTAIL` anywhere in the command bypasses it.

Only pipelines whose *producer* could be long-running count. A pipe is ignored
when it sits in a heredoc body, a quoted string, a `$(...)` substitution or the
condition of a `while`/`until` loop, and when the pipeline starts with a reader
or short-output tool (`cat`, `grep`, `git`, `ls`, `tmux`, `pgrep`, `curl`,
`squeue`, ...). `cmd | grep X | tail` still fires: `grep -v noise | tail` is a
real truncation, and the no-filter variant lost 5 genuine saves in the audit
that produced this rule (see ENGINEERING_LOGS.md, 2026-08-28).
"""
import json
import re
import sys

READERS = {
    "cat", "tac", "grep", "egrep", "fgrep", "rg", "ag", "ls", "ll", "tree", "find", "fd",
    "git", "gh", "tmux", "echo", "printf", "wc", "sed", "awk", "sort", "uniq", "jq", "yq",
    "head", "tail", "cut", "tr", "column", "pgrep", "ps", "pstree", "lsof", "ss", "netstat",
    "curl", "wget", "du", "df", "env", "printenv", "which", "type", "file", "stat", "diff",
    "delta", "strings", "od", "xxd", "hexdump", "base64", "date", "uptime", "free", "nvidia-smi",
    "whowas", "sqlite3", "squeue", "sacct", "sinfo", "journalctl", "dmesg", "true", "false",
    "test", "[",
}

PIPE_TAIL = re.compile(r"\|\s*(tail|head)\b")
HEREDOC = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n(.*?)\n\s*\1\s*(?=\n|$)", re.S)
QUOTES = re.compile(r"'[^'\n]*'|\"(?:\\.|[^\"\\\n])*\"")
SUBST = re.compile(r"\$\((?:[^()]|\([^()]*\))*\)")
LOOP_COND = re.compile(r"\b(?:while|until)\b(.*?)(?:;\s*|\n\s*)do\b", re.S)
# statement separators; the `&` alternative must not match the one in `2>&1` / `&>`
SPLIT = re.compile(r"&&|\|\||;|\n|(?<![|&<>])&(?![&|>])|\bthen\b|\bdo\b|\belse\b|\bdone\b|\bfi\b")
WRAPPER = re.compile(
    r"^(?:[A-Za-z_]\w*=\S*\s+|sudo\s+|command\s+|builtin\s+|time\s+|nohup\s+|env\s+"
    r"|nice\s+(?:-n\s*\d+\s+)?|timeout\s+(?:-\S+\s+)*\S+\s+)"
)
WORD = re.compile(r"[\w./\-\[]+")


def producer_of(stage: str) -> str:
    """Base name of the command a pipeline stage runs, minus env assignments and wrappers."""
    seg = stage.strip().lstrip("(")
    while True:
        m = WRAPPER.match(seg)
        if not m:
            break
        seg = seg[m.end():]
    m = WORD.match(seg)
    return m.group(0).rsplit("/", 1)[-1] if m else ""


def strip_inert(command: str) -> str:
    """Blank out text in which a `|` is not a live shell pipe of this command."""
    s = HEREDOC.sub(
        lambda m: m.group(0).split("\n", 1)[0] + "\n" + " " * len(m.group(2)) + "\n" + m.group(1),
        command,
    )
    s = QUOTES.sub(lambda m: " " * len(m.group(0)), s)
    s = SUBST.sub(lambda m: " " * len(m.group(0)), s)
    s = LOOP_COND.sub(lambda m: m.group(0).replace("|", " "), s)
    return s


def should_fire(command: str) -> bool:
    if "NEEDTAIL" in command:
        return False
    s = strip_inert(command)
    if not PIPE_TAIL.search(s):
        return False
    for statement in SPLIT.split(s):
        if not PIPE_TAIL.search(statement):
            continue
        stages = [st for st in statement.split("|") if st.strip()]
        if len(stages) < 2:
            continue
        producer = producer_of(stages[0])
        if producer and producer not in READERS:
            return True
    return False


def main():
    data = json.load(sys.stdin)
    if data.get("tool_name") != "Bash":
        return
    tool_input = data.get("tool_input", {})
    if not tool_input.get("run_in_background"):
        return
    if not should_fire(tool_input.get("command", "")):
        return
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": (
                        "Background commands write output to a file, so piping through "
                        "tail/head loses the rest. Drop the pipe and read/grep/slice/tail/head "
                        "the output file afterwards instead — this keeps e.g. stack traces of "
                        "long tasks. If piping is genuinely needed (e.g. polling), add NEEDTAIL "
                        "as a comment in the command to bypass this hook."
                    ),
                }
            }
        )
    )


if __name__ == "__main__":
    main()
