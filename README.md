# claude-code-hooks

The hook suite behind a heavily customized [Claude Code](https://claude.ai/code)
harness: guards against known agent failure modes, background-task hygiene,
teammate/multi-agent workflow enforcement, config generation, and
conversation backups. Battle-tested daily; published as working reference
material rather than a turnkey framework — wire in the ones you want.

Companion repos: [claude-code-patches](https://github.com/Butanium/claude-code-patches)
(byte patches for the Claude Code binary, applied by a SessionStart hook) and
[custom-claude-mcps](https://github.com/Butanium/custom-claude-mcps) (small MCP
servers this suite pairs with — notably `team-inbox`, whose `inbox_state.py`
helper `pending_message_guard.py` imports from `~/.claude/mcp/team-inbox/`, and
whose `teammate_status` tool reads the state `inflight_tracker.py` maintains).

## The hooks

Hooks are invoked individually from `settings.json` — nothing scans this
folder. Adopt à la carte.

### Session start

| Hook | What it does |
|---|---|
| `sync_config.py` | Syncs the `~/.claude` config repo on session start so every machine converges: auto-commits journal files (`*_journal.md`, without touching anything else the user has staged), pulls (rebase when local commits exist and the index is clean, ff-only otherwise; a conflicted rebase is aborted rather than left half-done), pushes anything ahead, updates submodules. Submodule updates are guarded: `git submodule update` checks out the recorded commit and detaches HEAD, so a submodule holding commits the gitlink doesn't reach would lose them from view (and since committing in a submodule leaves you detached by default, often with no branch ref to recover from). The hook initializes what's missing, fast-forwards what is strictly behind, and skips anything ahead, diverged, conflicted or dirty — printing what it declined to touch on stdout, which on SessionStart lands in the session's context. Covered by `tests/test_sync_submodules.py`. Recommended companion: a `.gitattributes` with `*_journal.md merge=union` so concurrent journal appends from two machines merge instead of conflicting. |
| `detect_env.py` | Detects which machine/environment this is (`env-configs/rules.json`), generates `CLAUDE.md` from `CLAUDE.template.md` + the env's config, emits `environment.json`, greets the model with per-model quirk notes (`model-quirks/*.md`) plus a pointer to the model's self-observation journal (`model-quirks/<key>_journal.md` — written by instances, promoted to the quirk notes by the human), and warns about unset personal-config values (see below). |
| `backup_conversations.py` | Async daily backup of `~/.claude/projects/` (all transcripts) to a private HuggingFace dataset repo, with in-memory secret redaction (HF/OpenAI/Anthropic/GitHub/AWS/Google token patterns) so HF's server-side secret scanner accepts the commits. Additive-only: a deletion-resistant backup, not a mirror. |
| `inflight_tracker.py` | (also PostToolUse + UserPromptSubmit) Tracks in-flight background work per session in `~/.claude/state/`, so other tooling can tell "idle" from "waiting on a background job". |

### Tool guards (PreToolUse)

| Hook | Matcher | What it does |
|---|---|---|
| `security_guard.py` | Bash | Blocks obviously dangerous commands (`permissionDecision: "deny"`) and stops the agent loop (`continue: false`) so the human is pulled in. `continue: false` alone does *not* block — the tool runs first and the stop lands after. Also pings the human's ntfy hotline (`CLAUDE_HOTLINE_NTFY_TOPIC`, skipped when unset) with host / cwd / session id / full command, since both in-harness signals assume someone is watching the terminal. |
| `force_background_bash.py` | Bash | Sync-timeout policy for the main agent / teammates: a requested timeout > 60s is clamped to 30s when the CLI can auto-background the command at timeout (so the conversation stops blocking, nothing is killed), and *denied with advice* when it can't — `$VAR`/backtick redirect targets, `git`, leading `sleep`, unquoted heredocs, `<<'EOF' > out` — because for those the sync timeout is a hard SIGTERM. Subagents may not use `run_in_background` (they get no completion notification) unless the command carries `BACKGROUND_NEEDED`. Kill-class fixtures probed live against the CLI in `tests/test_force_background_bash.py`. |
| `force_background_sleep.py` | Bash | Auto-backgrounds `sleep`-as-watchdog commands so a task finishing early doesn't strand the agent on its own timer. |
| `no_tail_head_pipes.py` | Bash | Blocks `\| tail` / `\| head` on background commands when the pipe truncates a long command's own output — the output goes to a file to be read/grepped afterwards, so stack traces aren't lost. Pipes on readers (`cat`/`grep`/`git`/`tmux`/`pgrep`/…), inside `$(...)`, heredocs, quotes or a `while`/`until` condition don't count (an audit of the old any-pipe regex found ~35% of its denials were those). `NEEDTAIL` in the command bypasses. Covered by `tests/test_no_tail_head_pipes.py`. |
| `no_poll_background.py` | Read, Bash | Denies the doom-loop poll: re-reading a background task's output file in a tight no-yield loop before the task finished. Extensively documented in-file. |
| `force_background_task.py` | Agent | Auto-applies `run_in_background=true` to subagent spawns. |
| `teammate_guard.py` | Agent | **DISABLED — unwired from `settings.json` 2026-07-24** (rarely fires on recent claude versions; kept here, not in `deprecated/`, so re-enabling is one settings entry). Blocked Agent calls whose context says "teammate"/"colleague" (Levenshtein ≤ 2) but that don't set `name` — the difference between a full teammate and a limited subagent. Bypass tags documented in-file; the user-directed one is derived from `I_LOVE_BEING_A_USER` and intentionally rude. |
| `claude_md_edit_reminder.py` | Read | On reading a *generated* `CLAUDE.md`: non-blocking reminder that edits belong in the template / env-configs. |
| `pending_message_guard.py` | SendMessage | Blocks sending while an undelivered inbound message from the recipient is pending, so it isn't silently dropped. Also guards dict-form `shutdown_request` (don't shut down a teammate whose reply you haven't seen — bypass tag goes in its `reason` field); protocol *responses* pass unrestricted. |
| `deny_download_arxiv.py` | MCP tool | Example of blocking one MCP tool in favor of a preferred skill. |

### Lifecycle

| Hook | Event | What it does |
|---|---|---|
| `teammate_idle_nag.py` | TeammateIdle | Nags a teammate that went idle with an outstanding message from the lead and no in-flight background work (per `inflight_tracker` state). |
| `iamok_stop_guard.py` | Stop | Catches synthetic API-error stops so the agent confirms it actually finished rather than dying silently. |

`utils/` holds shared helpers imported by hooks (not hooks themselves).
`deprecated/` is the graveyard: retired hooks kept with a note on *why* each
was retired — often more useful than the code.

## Install

Requires [`uv`](https://docs.astral.sh/uv/) (deps in `pyproject.toml`, venv
auto-created on first run) — except where noted, hooks read the tool-call JSON
on stdin and answer via the [hook JSON protocol](https://docs.claude.com/en/docs/claude-code/hooks).

Clone (or submodule) into `~/.claude/hooks`, then wire the hooks you want in
`~/.claude/settings.json`. The invocation pattern:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "VIRTUAL_ENV= uv run --project \"${CLAUDE_CONFIG_DIR:-$HOME/.claude}\"/hooks \"${CLAUDE_CONFIG_DIR:-$HOME/.claude}\"/hooks/detect_env.py"
          }
        ]
      }
    ],
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "VIRTUAL_ENV= uv run --project \"${CLAUDE_CONFIG_DIR:-$HOME/.claude}\"/hooks \"${CLAUDE_CONFIG_DIR:-$HOME/.claude}\"/hooks/security_guard.py"
          }
        ]
      }
    ]
  }
}
```

(`VIRTUAL_ENV=` clears any active project venv so `uv run --project` resolves
the hooks' own environment.)

`detect_env.py` expects the template pipeline (`CLAUDE.template.md`,
`env-configs/`, optional `model-quirks/`) in `$CLAUDE_CONFIG_DIR` — skip it if
you don't generate your CLAUDE.md this way.

Besides `{{ENV_CONFIG}}`, the template (and env-configs) may contain
`{{INCLUDE:path}}` directives (path relative to `$CLAUDE_CONFIG_DIR`), replaced
at generation time with the file's content. Useful for sections that agents
append to directly (e.g. a running list of observations): they edit the small
standalone file instead of the master template, and the next regeneration picks
it up. Missing include → placeholder text + a session-start warning; empty file
→ a "nothing here yet" placeholder. Non-recursive.

## Personal config — nothing personal in the sources

This folder is public, so anything user- or machine-specific is injected via
config:

| Value | Where it lives | Used for |
|---|---|---|
| `I_LOVE_BEING_A_USER` | `settings.json` `env` block | Your name, as hooks should use it when talking to Claude about you (fallback: "the user"). Also derives `teammate_guard`'s user bypass tag. |
| `CLAUDE_CODE_BACKUP_REPO_NAME` | env var, **or** `backup_repo_name` in `environment.json` (generated per-machine from `env-configs/<env>.json`) | HF dataset repo for conversation backups. Required — unset means backups are skipped and `detect_env.py` warns at session start. Per-machine by design; if you'd rather not put machine names in repo names, use e.g. a keyed hash of the hostname. The HF *namespace* is resolved at runtime from your token (`whoami`). |
| `CLAUDE_CODE_BACKUP_DISABLED` | env var | Set to anything to opt out of backups *and* the warning. |
| `CLAUDE_NOTIFS_NTFY_TOPIC` / `CLAUDE_HOTLINE_NTFY_TOPIC` | shell profile (secret-ish, outside any repo) | ntfy.sh topics the harness instructions reference for reaching the human. Hooks only check that they're set. |

## Conventions

- **Fail loud.** Guards that can't parse their input crash with a traceback
  (non-blocking) rather than silently approving.
- **Every blocking hook has a string opt-out** documented in its deny message,
  so a false positive costs one retry, not a dead end.
- **Nothing personal in sources** — see above. If you add a hook that needs a
  personal value, thread it through config the same way.
- **Deprecation over deletion** — retired hooks move to `deprecated/` with a
  why-note in its `CLAUDE.md`.

## License

MIT
