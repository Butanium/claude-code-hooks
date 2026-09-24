#!/bin/bash
# End-to-end smoke for agent_worktree_teammate.py + the teammate-cwd CLI patch.
#
# Asks a haiku lead (in a scratch tmux session) to spawn a named Agent with
# isolation "worktree", then checks what actually happened: the tool result is
# teammate_spawned, the team config records a tmux backend with the worktree as
# cwd, and the teammate's tmux pane sits in that worktree. Neither "the patch
# applied" nor "the hook emitted updatedInput" proves this; after a claude update
# only this does. ~$0.2, ~1 min.
#
#   tests/smoke_agent_worktree_teammate.sh            # patch a copy of the live binary
#   tests/smoke_agent_worktree_teammate.sh --keep     # leave tmux session + repo for inspection
set -euo pipefail

KEEP=0; [ "${1:-}" = "--keep" ] && KEEP=1
PATCHES="$HOME/.claude/scripts/cli-patches"
PATCH=
for p in "$PATCHES"/patches/teammate-cwd.py "$PATCHES"/experimental/teammate-cwd.py; do
    [ -f "$p" ] && { PATCH=$p; break; }
done
[ -n "$PATCH" ] || { echo "FAIL: teammate-cwd.py not found under $PATCHES"; exit 1; }
W=/var/tmp/tcwd-smoke; REPO=$W/repo; SESS=tcwd-smoke
BIN=$W/claude-smoke
WT=$REPO/.claude/worktrees/probe

fail() { echo "FAIL: $*"; exit 1; }
cleanup() {
    [ $KEEP -eq 1 ] && { echo "kept: tmux session $SESS, repo $REPO"; return; }
    tmux kill-session -t $SESS 2>/dev/null || true
    rm -rf "$REPO" "$BIN" "$BIN.orig"
}
trap cleanup EXIT

tmux kill-session -t $SESS 2>/dev/null || true
rm -rf "$W"; mkdir -p "$REPO"
cp "$(readlink -f "$(command -v claude)")" "$BIN"; chmod +x "$BIN"
CLAUDE_CLI_PATCH_TARGET=$BIN python3 -B "$PATCH" || fail "teammate-cwd patch does not apply to $(claude --version)"
# A copy of an already-patched live binary gets no .orig (nothing to apply) and
# already runs those modules from source.
[ -f "$BIN.orig" ] && CLAUDE_CLI_PATCH_TARGET=$BIN python3 -B "$PATCHES/patches/zz-bytecode-off.py"

git -C "$REPO" init -q; echo smoke > "$REPO/README.md"; git -C "$REPO" add .; git -C "$REPO" commit -q -m init
start=$(date +%s)
tmux new-session -d -s $SESS -x 200 -y 50 -c "$REPO" "$BIN --model haiku"

for _ in $(seq 30); do
    sleep 1; pane=$(tmux capture-pane -p -t $SESS)
    if grep -q "trust this folder" <<<"$pane"; then
        tmux send-keys -t $SESS Down; sleep 0.3; tmux send-keys -t $SESS Enter
    elif grep -q "^❯" <<<"$pane"; then break; fi
done
sleep 2
tmux send-keys -t $SESS -l 'Plumbing test. Make exactly one tool call: the Agent tool with name "probe", isolation "worktree", description "cwd probe", and prompt "Send the lead one SendMessage containing your Primary working directory from your environment section, then stop. Use no other tools." Use no other tool.'
sleep 0.5; tmux send-keys -t $SESS Enter

proj=$HOME/.claude/projects/$(sed 's#[/.]#-#g' <<<"$REPO")
for _ in $(seq 120); do
    sleep 1
    lead=$(find "$proj" -maxdepth 1 -name '*.jsonl' -newermt "@$start" -print -quit 2>/dev/null || true)
    [ -n "$lead" ] || continue
    cfg=$HOME/.claude/teams/session-$(basename "$lead" | cut -c1-8)/config.json
    [ -f "$cfg" ] && jq -e '.members[] | select(.name=="probe")' "$cfg" >/dev/null 2>&1 && break
done
[ -n "${cfg:-}" ] && [ -f "$cfg" ] || fail "no team config for the lead (lead transcript: ${lead:-none})"
member=$(jq -c '.members[] | select(.name=="probe") | {cwd, backendType, tmuxPaneId}' "$cfg")
[ -n "$member" ] || fail "no 'probe' member after 120 s — the call may have gone in-process; see $lead"
status=$(jq -r 'select(.toolUseResult.name?=="probe") | .toolUseResult.status' "$lead" | head -1)

[ "$status" = "teammate_spawned" ] || fail "tool result status '$status', want teammate_spawned"
[ "$(jq -r .backendType <<<"$member")" = tmux ] || fail "backend $(jq -r .backendType <<<"$member"), want tmux"
[ "$(jq -r .cwd <<<"$member")" = "$WT" ] || fail "team config cwd $(jq -r .cwd <<<"$member"), want $WT"
pane_id=$(jq -r .tmuxPaneId <<<"$member")
pane_cwd=$(tmux list-panes -a -F '#{pane_id} #{pane_current_path}' | awk -v p="$pane_id" '$1==p{print $2}')
[ "$pane_cwd" = "$WT" ] || fail "pane $pane_id is in '$pane_cwd', want $WT"
git -C "$REPO" worktree list | grep -q "$WT .*\[worktree-probe\]" || fail "no worktree-probe worktree at $WT"
echo "PASS: $(claude --version) — probe is a tmux teammate (pane $pane_id) in $WT"
