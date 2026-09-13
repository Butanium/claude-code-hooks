# Engineering logs

Append-only. What changed, why, and the gotcha — the reasoning that would
otherwise end up as a comment in the hook.

## 2026-09-12 — `force_background_bash.py`'s patch detector broke silently on 2.1.270

`binary_backgrounds_everything()` decides whether the CLI still has a kill class
at all. It looked for the patched shape beside the literal `&&!/git/i.test(`,
because on 2.1.250 the Bash tool computed its two flags side by side:

    Pt=!cn&&obr(Ce),gn=!cn&&!/git/i.test(Ce)

2.1.270 deleted that git test (`Xt=!Wn` now), so the anchor vanished from the
binary entirely. Nothing failed: the scan simply found no occurrence and
returned `False`, i.e. *every* patched binary read as unpatched, and the hook
went back to denying commands the patched CLI backgrounds perfectly well. A
detector whose negative result is indistinguishable from "genuinely unpatched"
is the failure mode to watch for here — there is no error to notice.

Now anchored on the property name `canAutoBackground:`, which is what the flag
is actually *for* and survives identifier renames: capture the flag's minified
name from it, then require that name's `=!<x>&&!0/*…*/,` assignment within 400B
before. Same anchor as `patches/auto-background.py` in the patches repo, and the
two must be re-anchored in lockstep — the hook is the only consumer that can
disagree with the patch about whether the patch is applied. Verified both ways
against the live binary (patched → True) and its pristine `.orig` (→ False);
`tests/test_force_background_bash.py` covers the pair.

Also `[\w$]` rather than `\w` for minified identifiers throughout. Minifiers
hand out `$`-containing names once the short pool runs out, and `\w` excludes
`$` — the same build renamed a local to `$e` in another module and killed a
patch in the patches repo the same way.

Second-order, not yet acted on: 2.1.270 also replaced the CLI's static shell
analyzer with a one-entry first-word blocklist (`["sleep"]`), so `KILL_CLASS_RE`
now over-approximates badly — it denies-with-advice for heredoc and redirect
shapes the CLI would background fine. That direction is safe (an advisory deny
costs a round trip, not work), so it is noted in the docstring rather than
rewritten blind; re-probe before trusting the deny branch.

## 2026-09-07 — `sync_config.py` was the fourth cp1252 casualty; plus a global `PYTHONUTF8`

The 2026-09-02 sweep below fixed the three hooks that *crashed*. `sync_config.py`
never crashed, so it was missed — cp1252 happens to have an em-dash at 0x97, so
its `— skipped` warnings encoded fine and only *rendered* as `�`, because Claude
Code decodes hook stdout as UTF-8. Silent corruption instead of a traceback is
why it survived the sweep. Now calls `utf8_stdio()` in `main()`.

Its two `subprocess.run(text=True)` git helpers had the mirror bug — git emits
UTF-8, the default decode was cp1252 — so any accented path or commit message
would have come back mojibake in `Synced: ...`. Both now pass
`encoding="utf-8", errors="replace"`.

Also set `PYTHONUTF8=1` in `settings.json` `env` as a backstop: it makes UTF-8
mode the default for every hook and every `subprocess` text pipe at once, so a
new hook that forgets `utf8_stdio()` is not a new bug. It does not replace the
per-hook calls — a hook run outside the Claude env (manually, from cron) still
needs its own. Upgrading Python would not have helped: `uv run --project hooks`
uses a uv-managed 3.13, unrelated to system Python, and UTF-8-by-default (PEP
686) is not in any release we can pin yet. `PYTHONUTF8=1` is what that default
will do anyway, so there is nothing to unwind later.

## 2026-09-02 — Windows cp1252 killed three hooks; explicit UTF-8 everywhere

Three separate hook failures on this box, one root cause and two hitchhikers.
All three surfaced only as `<Event> hook error` + a first stderr line reading
`Traceback (most recent call last):` — the terminal shows one line, so the
actual exception was invisible. The full stderr *is* kept: it lands in the
session JSONL as an attachment with `type: "hook_non_blocking_error"`, carrying
`hookName`, `command`, `exitCode` and the whole `stderr`. That is where to look
when a hook error has no readable cause.

**cp1252, both directions.** Windows Python defaults stdout/stderr *and* text
file IO to the ANSI code page (`locale.getencoding() == "cp1252"` here, while
`sys.getfilesystemencoding()` is utf-8 — easy to misread as "we're fine").

- `detect_env.py` printed the `⚠️` env warning and died `UnicodeEncodeError`,
  two statements before `print(identity)` — so **every session that tripped the
  warning silently lost its whole session-start identity block**: the greeting,
  the model-quirks injection, the journal pointer. The hook error looked
  cosmetic and was eating a feature.
- `no_poll_background.py` read the transcript with the default decode and died
  `UnicodeDecodeError: byte 0x90`. Any transcript containing an emoji or an
  undefined-in-cp1252 byte broke the guard.

Fixed by `utils/_encoding.py::utf8_stdio()` (reconfigures stdout/stderr; call it
before printing non-ASCII) plus an explicit `encoding="utf-8"` on every text
`open()` / `read_text()` / `write_text()` in the package. `open()`'s default
cannot be changed from inside the process, so the explicit encoding is the fix,
not a belt-and-braces. Note cp1252 round-trips most UTF-8 byte sequences
unharmed (read→mojibake→write gives the original bytes back), which is why
CLAUDE.md generation *appeared* to work: the bug only bites on the bytes cp1252
leaves undefined (0x81, 0x8D, 0x8F, 0x90, 0x9D).

**`os.getuid()` in `force_background_bash.py`** — no such attribute on Windows,
so `binary_backgrounds_everything()` raised `AttributeError` and the whole hook
died on every Bash call in the kill class. Now guarded with `hasattr`; Windows
gets each user their own temp dir anyway.

**And with that path finally executing, two more bugs behind it**, both making a
patched binary read as unpatched (so kill-class commands got denied instead of
clamped):

1. This file's copy of the Bun-graph parser hard-coded the POSIX virtual-FS
   prefix `/$bunfs/`. Same bug the patches repo fixed in `_bungraph.py` on
   2026-09-02 — the copy here never got it. Now `_NAME_PREFIXES`, mirroring
   `NAME_PREFIXES` upstream. **This parser is duplicated across two repos and
   drifted; changes to either must be mirrored.**
2. `shutil.which("claude")` resolves to `…/commands/claude.CMD`, a one-line
   Windows launcher shim with no module graph in it. Added `_claude_binary()`,
   which dereferences the shim and falls back to `~/.local/bin/claude[.exe]`,
   mirroring `candidate_binaries()` in the patches repo.

**`check_env_vars()` false positive.** It required the post-rename names only,
so a box exporting the pre-rename `CLAUDE_NOTIFS_TOPIC` / `CLAUDE_HOTLINE_TOPIC`
— which `security_guard.py` still honours, and which CLAUDE.template.md still
tells agents to use — was warned at as unconfigured. That bogus warning is what
carried the `⚠️` that killed the hook. `LEGACY_ALIASES` now accepts both.
Open question for the template: it documents the old names, so either it or the
shell profile should move to the `*_NTFY_TOPIC` pair.

## 2026-09-01 — ntfy topic env vars renamed to a `*_NTFY_TOPIC` suffix

`CLAUDE_HOTLINE_TOPIC` / `CLAUDE_NOTIFS_TOPIC` are now
`CLAUDE_HOTLINE_NTFY_TOPIC` / `CLAUDE_NTFY_TOPIC`, matching the
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
