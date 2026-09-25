#!/usr/bin/env python3
"""PostToolUseFailure(Bash) hook: when a failed command is an `&&` chain with a
file-changing step after an `&&`, say that those steps may not have run.

The failure it targets: `edit && build` or `grep -c X f && sed -i … f` exits
non-zero, the instance reads the error, fixes the obvious thing, and carries on as
if the later edit/write/commit had happened — building stale sources, or telling
the user a fix was pushed that never left the machine (archive sweep, 2026-09-25:
~14 sessions in six weeks; triggers included grep with no match exiting 1, a failed
cd, a hook deny, and `| tail` hiding a failed push).

It can't tell which step failed, so the note names the candidate steps instead of
claiming they were skipped. Silent when there is no `&&`, or no file-changing step
after one. Fail-open: any exception exits 0 with nothing printed.
"""
import json
import re
import sys

from no_tail_head_pipes import strip_inert

# statement separators, keeping which one each statement follows
SEP = re.compile(r"(&&|\|\||;|\n|(?<![|&<>])&(?![&|>]))")
NULL_REDIRECT = re.compile(r"\d?>&\d|&>\s*/dev/null|\d?>>?\s*/dev/null")
# Steps whose skipping leads to a wrong belief about files or remote state. Deliberately
# narrow: `cmd > run.log` (capturing output) and `python3 - <<EOF` (usually analysis, and
# usually the step that failed) made up most false firings in the archive replay.
CONTENT_WRITE = re.compile(r"^\s*(?:cat|printf|echo)\b[^|]*(?<![<\d])>>?(?!&)")
WRITE_STEP = re.compile(
    r"\bsed\s+(?:-\w+\s+)*-i|\btee\b|\bperl\s+-\w*i"
    r"|(?:^|\s)(?:mv|cp|ln|patch|truncate|install)\s"
    r"|\bgit\s+(?:commit|push|apply|checkout|restore|mv|rm|tag|merge|rebase|stash|cherry-pick|reset)\b"
    r"|\bgh\s+(?:pr|issue|release)\s+(?:create|comment|edit|merge)\b"
)


def write_steps_after_and(command: str) -> list[str]:
    """File-changing statements that come after an `&&` (so they only run if every
    step before them succeeded)."""
    s = strip_inert(command)  # same length as command: quotes/heredoc bodies blanked in place
    steps, after_and, start, op = [], False, 0, None
    matches = [*SEP.finditer(s)]
    delims = set(re.findall(r"<<-?\s*['\"]?(\w+)", command))

    def rest_is_empty(pos):  # nothing but blanked heredoc bodies and their delimiters
        return all(not ln.strip() or ln.strip() in delims for ln in s[pos:].splitlines())

    for i, m in enumerate([*matches, None]):
        end = m.start() if m else len(s)
        if op == "&&":
            after_and = True
        elif op in (";", "\n", "||"):
            after_and = False  # runs regardless (or only on failure)
        stmt = NULL_REDIRECT.sub("", s[start:end])
        # `cat > f <<EOF` itself almost never fails: when more steps follow it, the
        # failure was most likely one of them (writing a script, then running it).
        last = m is None or rest_is_empty(m.end())
        if after_and and (WRITE_STEP.search(stmt) or (last and CONTENT_WRITE.search(stmt))):
            steps.append(" ".join(command[start:end].split())[:80])
        if m:
            op, start = m.group(1), m.end()
    return steps


def main():
    data = json.load(sys.stdin)
    if data.get("tool_name") != "Bash" or data.get("is_interrupt"):
        return
    command = (data.get("tool_input") or {}).get("command") or ""
    steps = write_steps_after_and(command)
    if not steps:
        return
    listed = "; ".join(f"`{s}`" for s in steps[:3]) + (" …" if len(steps) > 3 else "")
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PostToolUseFailure",
        "additionalContext": (
            "This command failed partway through an `&&` chain, so every step after the "
            f"failing one was skipped. File-changing steps that may not have run: {listed}. "
            "Check that they took effect before building, testing or reporting on them."
        ),
    }}))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        sys.exit(0)
