#!/usr/bin/env python3
"""PreToolUse hook: denies a `pkill -f PAT` (or a `pgrep -f PAT` whose pids reach
a kill) when the pattern matches the command's own text, because that kills the
Bash tool's own shell.

The Bash tool runs every command as `/bin/bash -c "… eval '<command>'"`, so the
calling shell's argv holds the whole command, pattern included. pkill spares
itself but not its ancestors: exit 143/144, no output, nothing after the pkill
runs. `-A` (--ignore-ancestors, recent procps-ng) fixes it; without it,
bracketing one character (`foo-ba[r]`) makes the regex stop matching its own
text. A self-matching `pgrep -f` gets a note when its status, count or captured
pids are used (`until ! pgrep -f X` never ends), and is left alone as a plain
listing. Replay against the session archive: ENGINEERING_LOGS.md, 2026-09-22.
"""
import getpass
import json
import os
import re
import shlex
import sys

from no_tail_head_pipes import HEREDOC

PUNCT = "();<>|&\n"
SIGNAL = re.compile(r"^-(\d+|[A-Z][A-Z0-9+-]+)$")
ARG_SHORT = set("dgGOPqrstuUF")
ARG_LONG = {
    "--cgroup", "--delimiter", "--euid", "--group", "--ns", "--nslist", "--older",
    "--parent", "--pgroup", "--pidfile", "--queue", "--runstates", "--session",
    "--signal", "--terminal", "--uid",
}
# Any of these makes the calling shell unmatched, or makes that uncertain; stay silent.
# Not -o/--oldest: with the target gone, the oldest match is the calling shell.
EXCLUDING = {
    "-A", "--ignore-ancestors", "-x", "--exact", "-v", "--inverse", "-O", "--older",
    "-g", "--pgroup", "-G", "--group", "-P", "--parent", "-s", "--session", "-t",
    "--terminal", "-F", "--pidfile", "-r", "--runstates", "--ns", "--cgroup",
}
SHELL_VAR = re.compile(r"\$[A-Za-z_{(]")
OPENERS = {"if", "while", "until", "for", "case", "{"}
CLOSERS = {"fi", "done", "esac", "}"}
CMD_START = {"do", "then", "else", "elif", "!"}


def tokens(command: str) -> tuple[list[str], set[int]]:
    """Shell words and operators (newline included), backticks stripped, plus the
    indices whose word opened a backtick."""
    # Heredoc bodies are mostly files and docs being written; a pkill in one fed to `bash <<EOF` goes unseen.
    command = HEREDOC.sub(lambda m: m.group(0).split("\n", 1)[0], command)
    lx = shlex.shlex(command, posix=True, punctuation_chars=PUNCT)
    lx.whitespace = " \t\r"
    lx.whitespace_split = True
    out, ticks = [], set()
    try:
        for t in lx:
            if t.startswith("`"):
                ticks.add(len(out))
            out.append(t.strip("`"))
    except ValueError:  # unbalanced quote further on; keep what was read before it
        pass
    return out, ticks


def is_op(tok: str) -> bool:
    return set(tok) <= set(PUNCT)


def is_redirect(tok: str) -> bool:
    return is_op(tok) and bool(set(tok) & set("<>"))


def base(tok: str) -> str:
    return tok.rsplit("/", 1)[-1]


def value_at(args: list[str], i: int) -> tuple[str, int]:
    """An option value starting at args[i] and the index of its last token; `$(...)` is one value."""
    if i >= len(args):
        return "", i
    if args[i] == "$" and i + 1 < len(args) and args[i + 1] == "(":
        depth, j = 0, i + 1
        while j < len(args):
            if is_op(args[j]):
                depth += args[j].count("(") - args[j].count(")")
                if depth <= 0:
                    break
            j += 1
        return "$(...)", j
    return args[i], i


def parse(args: list[str]) -> tuple[set[str], list[str], str | None]:
    """Flags, -u/-U values and pattern of one pkill/pgrep call, reading up to the first
    operator. Options may follow the pattern (`pkill -f foo -A`); getopt permutes them."""
    flags, users, pattern = set(), [], None
    i = 0
    while i < len(args):
        a = args[i]
        if is_op(a):
            break
        if a == "--":
            if pattern is None and i + 1 < len(args) and not is_op(args[i + 1]):
                pattern = args[i + 1]
            break
        if a.startswith("--"):
            name, eq, val = a.partition("=")
            flags.add(name)
            if name in ARG_LONG and not eq:
                val, i = value_at(args, i + 1)
            if name in ("--euid", "--uid"):
                users.append(val)
        elif SIGNAL.match(a):
            pass
        elif a.startswith("-") and len(a) > 1:
            for j, ch in enumerate(a[1:], 1):
                flags.add("-" + ch)
                if ch in ARG_SHORT:
                    val = a[j + 1:]
                    if not val:
                        val, i = value_at(args, i + 1)
                    if ch in "uU":
                        users.append(val)
                    break
        elif pattern is None:
            pattern = a
        i += 1
    return flags, users, pattern


def is_self(user_arg: str) -> bool:
    me = {os.environ.get("USER", ""), getpass.getuser(), str(os.getuid())}
    return "$" in user_arg or any(u in me for u in user_arg.split(","))


def self_matches(args: list[str], command: str) -> tuple[str, set[str]] | None:
    """(pattern, flags) if this pkill/pgrep call would match the shell running `command`."""
    flags, users, pattern = parse(args)
    if not flags & {"-f", "--full"} or flags & EXCLUDING:
        return None
    if users and not any(is_self(u) for u in users):
        return None
    if pattern is None or SHELL_VAR.search(pattern):
        return None
    try:
        regex = re.compile(pattern, re.I if flags & {"-i", "--ignore-case"} else 0)
    except re.error:
        return None
    return (pattern, flags) if regex.search(command) else None


def bracketed(pattern: str) -> str | None:
    """The pattern with its last plain letter or digit bracketed, e.g. `foo ba[r]`."""
    depth = 0
    last = None
    for i, ch in enumerate(pattern):
        escaped = i > 0 and pattern[i - 1] == "\\"
        if ch == "[" and not escaped:
            depth += 1
        elif ch == "]" and not escaped and depth:
            depth -= 1
        elif ch.isalnum() and not escaped and not depth:
            last = i
    if last is None:
        return None
    candidate = pattern[:last] + "[" + pattern[last] + "]" + pattern[last + 1:]
    return None if re.search(candidate, candidate) else candidate  # `[x]` still contains an x


def pipeline_end(toks: list[str], start: int) -> int:
    """Index of the separator that ends the pipeline containing toks[start]; loop and
    if bodies inside it (`| while read p; do kill $p; done`) belong to it."""
    depth, cmd_pos = 0, False
    for j in range(start, len(toks)):
        t = toks[j]
        if not is_op(t):
            if cmd_pos and t in OPENERS:
                depth += 1
            elif cmd_pos and t in CLOSERS:
                depth -= 1
                if depth < 0:
                    return j
            cmd_pos = t in CMD_START
            continue
        if is_redirect(t):
            continue
        depth += t.count("(") - t.count(")")
        if depth < 0:
            return j
        if depth == 0 and t != "|" and set(t) & set(";&\n"):
            return j
        cmd_pos = True
    return len(toks)


def kill_args(toks: list[str], j: int) -> list[str]:
    args = []
    for t in toks[j + 1:]:
        if is_op(t):
            break
        args.append(t)
    return args


def kills_var(toks: list[str], var: str, start: int) -> bool:
    refs = (f"${var}", f"${{{var}}}")
    for j in range(start, len(toks)):
        if base(toks[j]) != "kill":
            continue
        args = kill_args(toks, j)
        if "-0" not in args and any(r in a for a in args for r in refs):
            return True
    return False


def next_op(toks: list[str], j: int) -> int:
    """The first operator at or after j that isn't a redirection, skipping redirect targets."""
    while j < len(toks):
        if is_redirect(toks[j]):
            j += 2
        elif is_op(toks[j]):
            return j
        else:
            j += 1
    return len(toks)


def pgrep_use(toks: list[str], ticks: set[int], i: int, flags: set[str]) -> str | None:
    """How the command uses the pgrep at toks[i]: 'kill' when its pids can reach a kill,
    'test' when its status, count or pids are used otherwise, None for a plain listing."""
    op_at = next_op(toks, i + 1)
    op = toks[op_at] if op_at < len(toks) else None
    dollar = i >= 2 and toks[i - 1] == "(" and toks[i - 2].endswith("$")
    if dollar or i in ticks:
        k = i - 3 if dollar else i - 1  # the word before `$(` / the backtick
        sig = None
        if k >= 0 and toks[k].startswith("-"):
            sig, k = toks[k], k - 1
        if k >= 0 and base(toks[k]) == "kill" and sig != "-0":
            return "kill"
        var = None
        if dollar and toks[i - 2].endswith("=$"):
            var = toks[i - 2][:-2]
        elif dollar and i >= 5 and toks[i - 3] == "in" and toks[i - 5] == "for":
            var = toks[i - 4]
        if var and kills_var(toks, var, i + 1):
            return "kill"
        return "test"
    if op == "|":
        end = pipeline_end(toks, op_at)
        for j in range(op_at + 1, end):
            if base(toks[j]) != "kill":
                continue
            args = kill_args(toks, j)
            if "-0" not in args and ("xargs" in toks[op_at:j] or any("$" in a for a in args)):
                return "kill"
        if toks[op_at + 1:op_at + 2] == ["wc"]:
            return "test"
    if flags & {"-c", "--count"}:
        return "test"
    if op == "||" and toks[op_at + 1:op_at + 2] in (["true"], [":"]):
        return None
    if op in ("&&", "||") or (i > 0 and toks[i - 1] in ("if", "while", "until", "!")):
        return "test"
    return None


def check(command: str) -> tuple[str, str] | None:
    """('deny' | 'warn', pattern) for the most serious self-matching call, else None."""
    toks, ticks = tokens(command)
    warn = None
    for i, t in enumerate(toks):
        tool = base(t)
        if tool not in ("pkill", "pgrep"):
            continue
        hit = self_matches(toks[i + 1:], command)
        if hit is None:
            continue
        pattern, flags = hit
        use = "kill" if tool == "pkill" else pgrep_use(toks, ticks, i, flags)
        if use == "kill":
            return "deny", pattern
        if use == "test" and warn is None:
            warn = ("warn", pattern)
    return warn


def main():
    data = json.load(sys.stdin)
    if data.get("tool_name") != "Bash":
        return
    command = data.get("tool_input", {}).get("command", "")
    hit = check(command)
    if hit is None:
        return
    verdict, pattern = hit
    example = bracketed(pattern)
    fix = (
        "Add -A (--ignore-ancestors) to the pkill/pgrep call. Where pkill has no -A (older "
        "procps), bracket one character instead"
        + (f" (e.g. {example!r})" if example else "")
        + " so the regex stops matching its own text."
    )
    if verdict == "deny":
        out = {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"`-f {pattern!r}` would kill the shell running this command: the Bash tool runs "
                "commands as `bash -c \"… <command>\"`, so that shell's own command line contains "
                "the pattern, and pkill spares only itself, not its parent. The result is exit "
                "143/144 with no output, and nothing after the kill runs. " + fix
            ),
        }
    else:
        out = {
            "hookEventName": "PreToolUse",
            "additionalContext": (
                f"`pgrep -f {pattern!r}` also matches the shell running this command, whose "
                "command line contains the pattern (inside `$(…)`, the subshell matches too). So "
                "`pgrep -f … && …` is always true, `until ! pgrep -f …` and "
                "`while kill -0 $(pgrep -f …)` loops never end, and counts are off by one or "
                "two. " + fix
            ),
        }
    print(json.dumps({"hookSpecificOutput": out}))


if __name__ == "__main__":
    main()
