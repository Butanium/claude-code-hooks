# Engineering logs

Append-only. What changed, why, and the gotcha — the reasoning that would
otherwise end up as a comment in the hook.

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
