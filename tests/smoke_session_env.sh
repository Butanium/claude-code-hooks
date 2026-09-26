#!/bin/bash
# Live check for session_env.py: a `claude -p` launched from a tmux server that
# carries a STALE value of a (fake) key must see the secrets file's value in its
# Bash tool, and GNU find instead of the CLI's bfs function. Costs one short
# haiku session. Run: bash tests/smoke_session_env.sh
set -euo pipefail
HOOKS="$(cd "$(dirname "$0")/.." && pwd)"
T=$(mktemp -d); SOCK="sessenv-smoke-$$"
trap 'tmux -L "$SOCK" kill-server 2>/dev/null || true; rm -rf "$T"' EXIT
printf 'export FAKE_SESSION_ENV_KEY=from-the-file\n' > "$T/secrets"
cat > "$T/settings.json" <<EOF
{"hooks":{"SessionStart":[{"hooks":[{"type":"command","command":"VIRTUAL_ENV= uv run --project $HOOKS $HOOKS/session_env.py"}]}]}}
EOF
BIN=$(readlink -f "$(command -v claude)")
printf '%s' 'Run exactly this one Bash command, then reply with its output verbatim and nothing else: echo "KEY=$FAKE_SESSION_ENV_KEY"; type -t find' > "$T/q"
# The tmux server is started WITH the stale value, like a long-lived server whose env predates the file.
tmux -L "$SOCK" new-session -d -e FAKE_SESSION_ENV_KEY=stale-inherited -e CLAUDE_SECRETS_FILE="$T/secrets" \
  -e DISABLE_AUTOUPDATER=1 -c /var/tmp \
  "'$BIN' -p --model haiku --dangerously-skip-permissions --no-session-persistence --settings '$T/settings.json' \"\$(cat '$T/q')\" > '$T/out' 2>&1; touch '$T/done'"
for _ in $(seq 180); do [ -f "$T/done" ] && break; sleep 1; done
cat "$T/out"
grep -q '^KEY=from-the-file$' "$T/out"
grep -qx 'file' "$T/out"
echo "SMOKE OK"
