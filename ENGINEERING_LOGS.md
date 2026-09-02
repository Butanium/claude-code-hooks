# Engineering logs

Append-only. What changed, why, and the gotcha — the reasoning that would
otherwise end up as a comment in the hook.

## 2026-09-01 — ntfy topic env vars renamed to a `*_NTFY_TOPIC` suffix

`CLAUDE_HOTLINE_TOPIC` / `CLAUDE_NOTIFS_TOPIC` are now
`CLAUDE_HOTLINE_NTFY_TOPIC` / `CLAUDE_NOTIFS_NTFY_TOPIC`, matching the
`CLAB_NTFY_TOPIC` / `NOMIC_NTFY_TOPIC` pair already in the shell profile. The
old names contained no "ntfy" anywhere in `NAME=value`, so the obvious probe —
`env | grep -i ntfy` — returned the other two and looked like proof these were
unset. A session concluded exactly that and reported the harness instructions
as referencing undefined variables; a second grep of `~/.bashrc` for
`NTFY\|ntfy` confirmed the same false negative. Every topic var now shares the
one substring anyone greps for.

`security_guard.py` keeps a `LEGACY_HOTLINE_ENV` fallback: hooks inherit the
env of the claude process that spawned them, so sessions started before the
rename carry only the old name and would silently lose their out-of-band
alert. Removable once no pre-rename session is running.

## 2026-09-01 — `force_background_bash.py`: kill-class deny steps aside on a patched claude binary

The `auto-background.py` patch in Butanium/claude-code-patches makes every
Bash command backgroundable at sync timeout (it forces the CLI's eligibility
flag true), so on a patched binary the kill class does not exist and the deny
branch would only cost the agent a round-trip. `binary_backgrounds_everything()`
looks at the claude binary in use (`which claude`, or
`FORCE_BACKGROUND_BASH_CLAUDE_BIN`) and returns True only when the patched
code shape is present at the Bash-tool site AND that module runs from source:
Bun 1.4.1+ ships JSC bytecode per module and executes it without re-checking
the text, so a patched text whose bytecode is still enabled is inert (found
today — all text patches had been silently dead since 2.1.250). The bytecode
check is a ~40-line copy of the patch repo's `_bungraph.py` module-table
parser; the verdict is cached per (path, size, mtime) in the temp dir so the
215 MB read happens once per binary. Tests pin the env var to a missing path
for the stock-rules fixtures and add a branch assertion against the local
binary, whichever state it is in.

assumes: the module-table layout parsed here (52-byte records, table pointer
at trailer-24, section start aligned to 512 with a leading u64 byte count) —
verified on claude 2.1.214/233/250/257 linux. If Bun changes it, the parser
returns None, the detector says "stock", and the hook falls back to denying —
safe, just noisier.

## 2026-08-28 — `detect_env.py`: whole-line comments no longer leave a blank line

`strip_html_comments` was a single `re.sub(r"<!--.*?-->", "")`, so a template
line that was *only* a comment collapsed to an empty line. In
`CLAUDE.template.md` that's the common case — retired guidance is kept by
commenting out individual bullets — and each one punched a blank line into the
middle of the generated markdown list, splitting it. Regenerating dropped 30
such lines from `CLAUDE.md`.

Now two passes: a line-anchored one (`^[ \t]*<!--.*?-->[ \t]*(?:\n|\Z)`,
DOTALL+MULTILINE) that eats the trailing newline of a comment owning its whole
line — multi-line `<!--\n…\n-->` blocks included — then the original in-place
sub for comments sharing a line with real text. Blank lines *around* a comment
are left alone, so paragraph spacing is unchanged. Pinned by
`tests/test_strip_html_comments.py`.

## 2026-08-28 — `no_tail_head_pipes.py`: producer-aware rule replaces the any-pipe regex

The hook used to deny any background Bash call matching `\|\s*(tail|head)\b`.
An audit of its last 100 firings (transcript search → blocked command +
the agent's next ~6 tool calls → 10 Sonnet classifiers + 10 Sonnet skeptics)
answered two questions:

- **Do agents bypass it?** No. 87/100 follow-ups dropped the pipe and stayed
  in background; 1 was judged a real "same truncation via another route".
  Repeat denials in a row: 1/100 (11/347 over the hook's whole history).
- **Was the pipe truncating anything?** Only in 65/100. The other 35 were a
  `grep`/`cat` of a log the command had *already* redirected (17), a short
  sub-step in front of the real work — `git log | head`, `pgrep | head`,
  `npm run build | tail -1 && real_cmd` (16), or a `tmux capture-pane | tail`
  / `$(curl … | head -1)` poll loop (2). Cost: ~1 extra tool call per denial,
  and one case where the forced rewrite of an `until … | tail -5 | grep -q ERROR`
  poll made the grep scan the whole scrollback, match a stale line and report a
  server up that wasn't. Of the 65 real targets, 17 demonstrably used output
  beyond the tail window afterwards (stack traces, Playwright call logs).

New rule: blank heredoc bodies, quoted strings, `$(...)` and `while`/`until`
conditions; split into statements (without tripping on the `&` of `2>&1`);
fire only when a statement's pipeline ends in tail/head **and** its producer is
not a reader / short-output tool. Scored on the labelled 100: keeps 64/65 real
targets (loses `git rebase --continue | tail -5`), removes 28/35 false
positives; the 7 left are "structurally a target, output happened to be short"
(`uv pip install | tail -20`), invisible to any static rule. Over the 347
historical firings it fires on 63%.

Rejected variant: also exempting `cmd | grep X | tail` (on the grounds that the
hook never fired on `cmd | grep X` alone). It loses 5 genuine saves —
`grep -v noise | tail` is a real truncation — so `grep` counts only as a
*producer*, not as a filter stage.

Known gap (out of scope): `pytest > f.log; echo $(tail -1 f.log); rm f.log`
passes and loses everything.

Audit artifacts (private, the raw commands carry personal paths):
`~/.claude/scratch/tail-head-hook-audit/`, incl. `check_against_labels.py`
which re-scores the installed hook against the labels.

## 2026-08-28 — `force_background_bash.py`: heredoc kill-class narrowed to the shapes that actually kill

The hook's deny-with-advice for >60s sync timeouts treated any `<<` as
"the CLI will SIGTERM this at timeout instead of backgrounding it". A
session asked whether that warning was still true on CLI 2.1.250, so it was
re-probed (5s timeout, 12s `python3 -c 'time.sleep(12)'`, eleven draws across
the strata a static analyzer could care about). Still kills: a `$VAR` /
backtick redirect target (with or without a heredoc), `git` anywhere in the
chain, an **unquoted** `<<EOF` (even with no redirect), and a quoted
`<<'EOF' > out` with the redirect *after* the operator. Backgrounds fine:
literal redirect paths, and a quoted heredoc whose redirects all precede the
operator — `cmd > out.log 2>&1 <<'EOF'` — regardless of body content
(`$HOME`, backticks, pipes and `>` inside the body don't matter), alone, in a
`;`/`&&` chain, or followed by a later redirecting statement.

`is_kill_class` now delegates heredocs to `heredoc_kills`: unquoted delimiter
→ kill; quoted delimiter with any `<`/`>` later on the operator's line → kill;
otherwise not kill-class. `<<<` herestrings stay kill-class untested. The deny
text now tells the agent the safe form instead of "no heredoc combined with a
redirect", which was both too broad (the `> out <<'EOF'` form is fine) and
too narrow (unquoted heredocs kill without any redirect).

assumes: the analyzer's verdict is a pure function of the command string
(memory note `bash-timeout-kill-vs-background` traced it to a static
decomposition in the 2.1.216 bundle) and that these eleven probes on one
Linux box generalize. Re-probe after CLI updates — the fixtures in
`tests/test_force_background_bash.py` are the probe list; each is a 5s-timeout
Bash call whose expected outcome is its list.
