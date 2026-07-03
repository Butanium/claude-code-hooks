# Deprecated hooks

Hooks retired from `settings.json`. Kept per the deprecation protocol (reading old approaches without git archaeology; the *why* matters more than the code).

- `no_raw_agent_transcript.py` — PreToolUse/Read hook that blocked reading `*/subagents/agent-*.jsonl` files directly, pushing toward `check-agent` / `read_agent_transcript` instead. Deprecated 2026-06-10 (unwired from settings.json on 2026-05-19, commit `578827a`): deliberately removed — the guidance lives as prose in CLAUDE.template.md ("Checking on background agents"), and the `transcript-reader` MCP + `check-agent` subagent cover the workflow without hard-blocking.
- `shutdown_guard.py` — PreToolUse/SendMessage hook that blocked `shutdown_request` messages unless the reason carried `[I ASSUME CLÉMENT WON'T WANT TO DISCUSS/AUDIT YOU]`, forcing the team lead to confirm Clément wouldn't need to review the teammate first. Deprecated 2026-06-10 (unwired from settings.json on 2026-05-19, commit `578827a`): replaced by softer prose in CLAUDE.template.md (commit `f93120f`, "more flexible on teammate shutdown") telling teammates themselves to push back on premature shutdown_requests.
