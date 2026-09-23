#!/usr/bin/env python3
"""Regression tests for pkill_self_match.py.

DENY/WARN fixtures are shapes that kill (or mislead) the calling Bash-tool
shell; the first DENY one is the 2026-09-22 proxy restart that did. SILENT
fixtures are shapes that leave the shell alone. The live checks at the end
re-verify the hook's premise on this box: a nested `bash -c` running a
self-matching `pkill -f` dies, and the same call with `-A` survives.

Run: python3 tests/test_pkill_self_match.py
"""

import json
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / "pkill_self_match.py"
sys.path.insert(0, str(HOOK.parent))
from pkill_self_match import bracketed, check  # noqa: E402


def ok(label, cond, detail=""):
    print(f"{'ok  ' if cond else 'FAIL'} {label}" + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        sys.exit(1)


DENY = [
    ("the proxy restart", "pkill -u \"$USER\" -f 'claude-code-proxy serve'; for i in $(seq 40); do sleep 0.25; done; curl -s localhost:18765"),
    ("bare pkill -f on its own line", "pkill -f 'uvicorn app:main'"),
    ("signal first", "pkill -9 -f \"port 8890\" ; sleep 2 ; uv run uvicorn app --port 8890"),
    ("--signal with value", "pkill --signal KILL -f vllm.entrypoints"),
    ("--full long form", "pkill --full vllm.entrypoints"),
    ("combined -fu self", "pkill -fu \"$USER\" jupyter-lab"),
    ("-u numeric self", "pkill -u $(id -u) -f jupyter-lab"),
    ("in a subshell", "(pkill -f 'http.server 8000' || true) && echo restarted"),
    ("absolute path", "/usr/bin/pkill -f my_worker.py"),
    ("sudo", "sudo pkill -f my_worker.py"),
    ("pgrep into xargs kill", "pgrep -f 'train.py --run 3' | xargs -r kill"),
    ("kill $(pgrep)", "kill $(pgrep -f 'train.py --run 3') 2>/dev/null; echo done"),
    ("kill `pgrep`", "kill -9 `pgrep -f train.py`"),
    ("case-insensitive -i", "pkill -i -f MY_WORKER"),
    ("regex that matches its own text", "pkill -f 'serve.*8890'; echo"),
    ("pid captured, killed later", "PID=$(pgrep -f 'port 8877' | head -1); [ -n \"$PID\" ] && kill \"$PID\"; curl -s localhost:8877"),
    ("pgrep into while-read kill", "pgrep -f 'tinkerscope --port 8894' | while read pid; do kill \"$pid\"; done"),
    ("pgrep through awk into xargs kill", "pgrep -af 'python -u probe' | awk '{print $1}' | xargs -r kill 2>/dev/null"),
    ("warn-shaped pgrep before a pkill (review #305)", "OLD=$(pgrep -f 'claude-code-proxy serve'); echo $OLD; pkill -f 'claude-code-proxy serve'"),
    ("for-loop over pgrep, kill -STOP (review #313)", "for p in $(pgrep -f run_remaining.sh); do kill -STOP $p; done"),
    ("-o picks the shell once the target is gone", "pkill -o -f 'uvicorn app'"),
    ("bracket defeated by the same text later on", "PID=$(pgrep -f 'tinkerscope [-]-port' | head -1); kill $PID; systemd-run bash -c 'exec tinkerscope --port 8767'"),
    ("after a heredoc", "cat > run.sh <<'EOF'\nexec uvicorn app\nEOF\npkill -f 'uvicorn app'; bash run.sh"),
]

WARN = [
    ("pgrep as a liveness check", "pgrep -f 'uvicorn app' || uv run uvicorn app"),
    ("pgrep in an if", "if pgrep -f 'port 8794' >/dev/null; then echo up; fi"),
    ("pgrep counted", "pgrep -f modalwatch.deadman | wc -l"),
    ("pgrep captured", "PID=$(pgrep -f 'claude-code-proxy serve' | head -1); echo $PID"),
    ("kill -0 probe loop", "while kill -0 $(pgrep -f try_sample2.py) 2>/dev/null; do sleep 3; done"),
    ("until ! pgrep loop, redirect before the separator", "until ! pgrep -f 'apt-get install' >/dev/null; do sleep 3; done"),
    ("pgrep -c", "pgrep -c -f 'pytest tests/ui' || echo none"),
    ("pgrep -c with the status thrown away", "pgrep -fc 'sandboxes/nohook' || true"),
    ("count, then kill an unrelated variable", "pgrep -f foo | wc -l; kill $SERVER_PID"),
]

SILENT = [
    ("bracketed pattern", "pkill -f 'claude-code-prox[y] serve'"),
    ("-A", "pkill -A -f 'claude-code-proxy serve'"),
    ("--ignore-ancestors", "pkill --ignore-ancestors -f 'claude-code-proxy serve'"),
    ("no -f matches process names only", "pkill uvicorn"),
    ("-x exact", "pkill -x -f 'uvicorn app'"),
    ("other user", "sudo pkill -u root -f cron.daily"),
    ("pattern from a variable", "pkill -f \"$PATTERN\""),
    ("parent filter", "pkill -P 1234 -f worker"),
    ("pidfile", "pkill -F /run/app.pid"),
    ("mention inside quotes", "git commit -m \"note: pkill -f foo kills its own shell\""),
    ("grep for pkill", "grep -rn 'pkill -f' ~/docs"),
    ("killall is name-based", "killall -r 'uvicorn.*'"),
    ("bgwatch --pgrep is not pgrep", "bgwatch --pgrep 'train.py' --every 60"),
    ("unbalanced quote after an unrelated command", "echo it's fine"),
    ("pgrep with -A", "pgrep -A -f 'uvicorn app' || uv run uvicorn app"),
    ("pgrep -af listing", "pgrep -af my_worker.py"),
    ("list, kill explicit pids, list again", "pgrep -af 'tinkerscope --port 8823' | cat; kill 2384751 2385439; pgrep -af 8823 | cat"),
    ("options after the pattern: -A", "pkill -f 'uvicorn app' -A"),
    ("options after the pattern: other user", "pkill -f 'uvicorn app' -u root"),
    ("status thrown away", "pgrep -af 'uvicorn app' || true"),
    ("listing, then an unrelated pipe on the next line", "pgrep -af foo\nls | xargs kill"),
    ("kill by pid, then list survivors", "kill -9 3326800 2811755; sleep 1; pgrep -af first_token_histogram; echo done"),
    ("doc written through a heredoc", "cat >> notes.md <<'EOF'\nuse `pkill -f PAT` with care, it matches\nEOF\ngit add notes.md"),
    ("python heredoc mentioning pkill", "python3 - <<'EOF'\nprint('pkill -f foo')\nimport subprocess\nEOF"),
]

for label, cmd in DENY:
    ok(f"deny: {label}", (check(cmd) or ("",))[0] == "deny", repr(check(cmd)))
for label, cmd in WARN:
    ok(f"warn: {label}", (check(cmd) or ("",))[0] == "warn", repr(check(cmd)))
for label, cmd in SILENT:
    ok(f"silent: {label}", check(cmd) is None, repr(check(cmd)))

# the suggested bracketing must stop matching its own text but keep matching the target
for pat, target in [("claude-code-proxy serve", "claude-code-proxy serve --no-monitor"),
                    ("serve.*8890$", "python -m http.serve --port 8890")]:
    b = bracketed(pat)
    ok(f"bracketed {pat!r} -> {b!r}",
       b is not None and check(f"pkill -f '{b}'") is None and __import__("re").search(b, target))
ok("bracketed with no plain character", bracketed(".*") is None)
ok("bracketed single character still self-matches", bracketed("x") is None)


def run_hook(command, tool="Bash"):
    r = subprocess.run([sys.executable, str(HOOK)], input=json.dumps({"tool_name": tool, "tool_input": {"command": command}}),
                       capture_output=True, text=True, check=True)
    return json.loads(r.stdout)["hookSpecificOutput"] if r.stdout.strip() else None


out = run_hook(DENY[0][1])
ok("end to end: deny", out and out["permissionDecision"] == "deny" and "-A" in out["permissionDecisionReason"]
   and bracketed("claude-code-proxy serve") in out["permissionDecisionReason"], repr(out))
out = run_hook(WARN[0][1])
ok("end to end: warn", out and "permissionDecision" not in out and "-A" in out["additionalContext"], repr(out))
ok("end to end: silent", run_hook(SILENT[0][1]) is None)
ok("end to end: non-Bash tool", run_hook(DENY[0][1], tool="Read") is None)

# live premise check; the pattern is built at runtime so this process's argv never holds it
pat = "sle" + "ep 4247.25"
for flag, should_survive in [("", False), ("-A ", True)]:
    target = subprocess.Popen(pat.split(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    r = subprocess.run(["bash", "-c", f"pkill {flag}-f '{pat}'; echo survived"], capture_output=True, text=True)
    survived = "survived" in r.stdout
    target.wait(timeout=5)
    ok(f"live: `pkill {flag}-f` {'spares' if should_survive else 'kills'} the calling shell",
       survived == should_survive and target.returncode != 0, f"rc={r.returncode} out={r.stdout!r}")

print("all passed")
