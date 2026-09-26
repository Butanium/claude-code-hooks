#!/usr/bin/env python3
"""Daily backup of ~/.claude/projects/ to a private HuggingFace dataset repo.

One repo per machine, grouped under a private "Claude Code Backups"
collection. The namespace is whoever the local HF token belongs to
(`whoami()`). The repo name is required and comes from (first hit wins):
the `CLAUDE_CODE_BACKUP_REPO_NAME` env var, or the `backup_repo_name` key
of `$CLAUDE_CONFIG_DIR/environment.json` (generated per-machine by
detect_env.py from env-configs/ — the right place for per-machine values
when settings.json is synced across machines). Neither set → backup is
skipped with a log line, and detect_env.py warns at session start.
Set `CLAUDE_CODE_BACKUP_DISABLED=1` to opt out silently (no backup, no
warning). Pick a per-machine name; if you'd rather not have machine names
appear in repo names, use e.g. a keyed hash of the hostname.

Runs on SessionStart (async). Sleeps 5 min so quick sessions skip the upload.

Upload strategy: list remote (with sizes), diff against local, push files that
are missing remotely or changed since their last upload, in fixed-size batches
via create_commit. "Changed" is tracked in a local manifest (DEBUG_DIR/
backup_manifest.json: path -> [size, mtime_ns] as of the last upload), because
transcripts are appended to for as long as a session lives: a path-only diff
uploads a session once, on the first day it exists, and never again. Without a
manifest entry (first run, manifest lost) a file on the remote counts as
current iff its redacted size equals the remote size. A file smaller than its
last upload is never pushed: the backup exists to survive local truncation.

Uploaded: transcripts (*.jsonl, subagents included), plus the *.json / *.txt /
*.md that sit beside them — subagent .meta.json, tool-results/ (large tool
outputs the transcript only references), workflow state, and per-project
auto-memory (memory/*.md), which nothing else backs up. Each file is redacted in-memory before
upload — known secret patterns (HF/OpenAI/Anthropic/GitHub/AWS/Google/Modal tokens, and any
value assigned to a *_SECRET / *_TOKEN / *_KEY variable or to a name in $CLAUDE_SECRETS_FILE)
are replaced with `<prefix><first-4-chars>_REDACTED` so HF's server-side
secrets scanner accepts the commit. Local files are never modified.

Retry policy: on 5xx we retry the same batch (transient infra). On a 400
flagged by HF's scanner we parse the offending file(s) out of the body,
drop them from the batch, and retry — that path covers secret patterns
our local regex missed. On 429 we bail cleanly; the next run resumes.

Sync semantics: additive only. We never delete from the remote — locally
removed or renamed transcripts stay on HF forever. This is intentional:
the dataset is a deletion-resistant backup, not a mirror.
"""

import argparse
import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock, Timeout

# Token patterns we redact in-memory before upload. The replacement keeps the
# prefix + first 4 chars of the body so the user can still distinguish which
# token leaked, but is short enough that HF's scanner won't re-validate it.
REDACTION_RULES = [
    ("sk-ant",       rb"(sk-ant-)([A-Za-z0-9_\-]{4})[A-Za-z0-9_\-]{16,}", rb"\1\2_REDACTED"),
    ("hf",           rb"(hf_)([A-Za-z0-9]{4})[A-Za-z0-9]{16,}",           rb"\1\2_REDACTED"),
    ("openai",       rb"(sk-)([A-Za-z0-9_\-]{4})[A-Za-z0-9_\-]{16,}",     rb"\1\2_REDACTED"),
    ("github-pat",   rb"(ghp_)([A-Za-z0-9]{4})[A-Za-z0-9]{32,}",          rb"\1\2_REDACTED"),
    ("github-oauth", rb"(gh[osu]_)([A-Za-z0-9]{4})[A-Za-z0-9]{32,}",      rb"\1\2_REDACTED"),
    ("aws-access",   rb"(AKIA)([0-9A-Z]{4})[0-9A-Z]{12}",                 rb"\1\2_REDACTED"),
    ("google-api",   rb"(AIza)([0-9A-Za-z_\-]{4})[0-9A-Za-z_\-]{31}",     rb"\1\2_REDACTED"),
    ("modal",        rb"(?:(?<=\\[nrt])|(?<![A-Za-z0-9]))(a[ks]-)([A-Za-z0-9]{4})[A-Za-z0-9]{16,}", rb"\1\2_REDACTED"),
    ("runpod",       rb"(rpa_)([A-Za-z0-9]{4})[A-Za-z0-9]{16,}",          rb"\1\2_REDACTED"),
]
_COMPILED_REDACTORS = [(name, re.compile(pat), repl) for name, pat, repl in REDACTION_RULES]

# Beyond known token shapes: the VALUE assigned to a secret-looking variable, in the forms a
# transcript carries it — `export X=v` / `X=v` (env dumps, .env cats, commands) and JSON
# `"X": "v"`, whose quotes are backslash-escaped when the JSON sits inside a transcript
# string. X is any UPPER_CASE name ending in one of SECRET_NAME_SUFFIXES (_API_KEY is a
# _KEY), or a name defined in the file $CLAUDE_SECRETS_FILE points at — only the NAMES are
# read from it; its values never enter this process. The env form is `NAME=value` with no
# spaces (env dumps, .env, export) — code constants (`const X_KEY = "…"`) use spaces. A value
# must be 8+ chars, hold a digit and a letter, and not look like a reference (`$VAR`,
# `OTHER_VAR`): a 2026-09-25 census found `*_KEY` code constants (storage keys, metadata
# keys) outnumbering real credentials until these two filters.
SECRET_NAME_SUFFIXES = (b"_SECRET", b"_TOKEN", b"_KEY")


def _secrets_file_names() -> list[bytes]:
    path = os.environ.get("CLAUDE_SECRETS_FILE")
    if not path:
        return []
    try:
        text = Path(path).expanduser().read_bytes()
    except OSError:
        return []
    names = re.findall(rb"^[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)[ \t]*=", text, re.M)
    return sorted(set(names))


_ASSIGNMENT_RES: list[re.Pattern] | None = None


def _assignment_res() -> list[re.Pattern]:
    global _ASSIGNMENT_RES
    if _ASSIGNMENT_RES is None:
        alts = [rb"[A-Z][A-Z0-9_]*(?:" + b"|".join(SECRET_NAME_SUFFIXES) + rb")"]
        alts += [re.escape(n) for n in _secrets_file_names()]
        name = rb"(?:" + b"|".join(alts) + rb")"
        q = rb"(?:\\?[\"'])?"  # an optional quote, maybe JSON-escaped
        _ASSIGNMENT_RES = [
            # a word boundary, or a JSON escape (`\nNAME=` in an escaped env dump)
            re.compile(rb"((?:(?<=\\[nrt])|(?<![A-Za-z0-9_]))" + name + rb"=" + q
                       + rb")([^\s\"'\\$`;&|<>()]{8,})"),
            re.compile(rb"(\\?\"" + name + rb"\\?\"[ \t]*:[ \t]*\\?\")([^\"\\]{8,})"),
        ]
    return _ASSIGNMENT_RES


def _redact_assignment(m: re.Match) -> bytes:
    value = m.group(2)
    if re.fullmatch(rb"[A-Z][A-Z0-9_]*_[A-Z0-9_]+", value):  # another variable's name
        return m.group(0)
    if not (re.search(rb"[0-9]", value) and re.search(rb"[A-Za-z]", value)):
        return m.group(0)  # `X_KEY = "tinkerscope-history"`: a code constant, not a credential
    return m.group(1) + value[:4] + b"_REDACTED"
BACKUP_SUFFIXES = (".jsonl", ".json", ".txt", ".md")
# Matches "- <path> (ref:" inside HF's 400 secrets-scanner response body.
_OFFENDING_FILE_RE = re.compile(r"-\s+(\S+\.(?:jsonl|json|txt|md))\s+\(ref:")


def redact_secrets(data: bytes) -> tuple[bytes, dict[str, int]]:
    """Return (redacted_bytes, {pattern_name: count}). Empty dict if nothing matched."""
    counts: dict[str, int] = {}
    for name, pattern, replacement in _COMPILED_REDACTORS:
        new_data, n = pattern.subn(replacement, data)
        if n:
            counts[name] = n
        data = new_data
    for pattern in _assignment_res():
        before = data
        data = pattern.sub(_redact_assignment, data)
        if data != before:
            n = sum(1 for m in pattern.finditer(before) if _redact_assignment(m) != m.group(0))
            counts["secret-assignment"] = counts.get("secret-assignment", 0) + n
    return data, counts

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
PROJECTS_DIR = CLAUDE_DIR / "projects"
DEBUG_DIR = CLAUDE_DIR / "debug"
STAMP_FILE = DEBUG_DIR / "backup_stamp.json"
MANIFEST_FILE = DEBUG_DIR / "backup_manifest.json"
COLLECTION_SLUG_FILE = DEBUG_DIR / "backup_collection_slug.txt"
LOCK_FILE = DEBUG_DIR / "backup.lock"

BACKUP_ENABLED = True  # kill switch: flip to False to make the hook a no-op
# Wall-clock cap on do_backup(); the watchdog hard-exits past this.
MAX_BACKUP_SECONDS = 25 * 60

# Upload tuning. Empirically 100-file batches return clean HF responses
# (400 with parseable body on secret hits) while larger batches sometimes
# trigger CloudFront timeouts (502) that hide the underlying scanner verdict.
BATCH_SIZE = 100
MAX_BATCH_RETRIES = 5
# Budget against attempts (success + retries) since failed attempts also
# count against HF's per-hour commit cap. 80 leaves buffer below the 128 limit.
MAX_ATTEMPTS_PER_RUN = 80
INITIAL_BACKOFF_SECONDS = 5
# Safety net inside _commit_batch in case 400-then-drop loops on us forever.
MAX_COMMIT_ITERATIONS = 20

COLLECTION_TITLE = "Claude Code Backups"
COLLECTION_DESCRIPTION = "Daily backups of ~/.claude/projects/ from each machine."

BACKUP_DISABLED = bool(os.environ.get("CLAUDE_CODE_BACKUP_DISABLED", "").strip())


def resolve_repo_name() -> str:
    """Repo name from env var, else environment.json's backup_repo_name, else "".

    The name is required config: it lives outside the source (and outside the
    machine-synced settings.json) so neither account nor machine names leak
    into this public file, and each machine keeps its own backup repo.
    """
    name = os.environ.get("CLAUDE_CODE_BACKUP_REPO_NAME", "").strip()
    if name:
        return name
    env_json = CLAUDE_DIR / "environment.json"
    if env_json.exists():
        try:
            return str(json.loads(env_json.read_text(encoding="utf-8")).get("backup_repo_name", "") or "").strip()
        except (json.JSONDecodeError, OSError):
            return ""
    return ""


def log(msg: str) -> None:
    """Print timestamped debug message to stderr."""
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[backup {ts}] {msg}", file=sys.stderr, flush=True)


def needs_backup() -> bool:
    """Return True if today's backup hasn't been done yet."""
    if not STAMP_FILE.exists():
        return True
    data = json.loads(STAMP_FILE.read_text(encoding="utf-8"))
    last = data.get("last_backup_date", "")
    return last != datetime.now(timezone.utc).strftime("%Y-%m-%d")


def start_watchdog(deadline_sec: int) -> None:
    """Spawn a daemon thread that hard-exits the process after deadline_sec.

    Uses os._exit so we bypass huggingface_hub's internal retry loop, which has
    no native max-retry knob. The thread is a daemon so it can't keep the process
    alive past a clean exit.
    """
    def _kill_if_late():
        time.sleep(deadline_sec)
        log(f"Watchdog: backup exceeded {deadline_sec}s, exiting hard")
        os._exit(2)

    threading.Thread(target=_kill_if_late, daemon=True).start()


def acquire_lock():
    """Return an acquired FileLock, or None if another instance holds it.

    Uses filelock for cross-platform (Linux/macOS/Windows) advisory locking.
    The lock is released automatically when the process exits, so killed/crashed
    runs don't leave a stale lock. Caller must keep the returned object alive.
    """
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(str(LOCK_FILE))
    try:
        lock.acquire(timeout=0)
    except Timeout:
        return None
    return lock


def get_or_create_collection(api, namespace: str) -> str:
    """Return slug of the private 'Claude Code Backups' collection, creating it if missing.

    Cached in COLLECTION_SLUG_FILE so subsequent runs skip the list/create call.
    """
    if COLLECTION_SLUG_FILE.exists():
        slug = COLLECTION_SLUG_FILE.read_text(encoding="utf-8").strip()
        if slug:
            return slug

    for c in api.list_collections(owner=namespace):
        if c.title == COLLECTION_TITLE:
            log(f"Found existing collection {c.slug}")
            COLLECTION_SLUG_FILE.parent.mkdir(parents=True, exist_ok=True)
            COLLECTION_SLUG_FILE.write_text(c.slug, encoding="utf-8")
            return c.slug

    log(f"Creating private collection '{COLLECTION_TITLE}' under {namespace}")
    collection = api.create_collection(
        title=COLLECTION_TITLE,
        namespace=namespace,
        description=COLLECTION_DESCRIPTION,
        private=True,
    )
    COLLECTION_SLUG_FILE.parent.mkdir(parents=True, exist_ok=True)
    COLLECTION_SLUG_FILE.write_text(collection.slug, encoding="utf-8")
    return collection.slug


def _commit_batch(
    api, hf_repo: str, batch: list[Path], commit_message: str
) -> tuple[int, list[Path], dict[str, int]]:
    """Redact files in-memory and commit them, handling 5xx and 400 distinctly.

    Returns (attempts_made, files_actually_committed, redaction_counts).
    - 5xx: retry the same batch with exponential backoff, up to MAX_BATCH_RETRIES.
    - 400 with "Offending files" body: parse the flagged paths, drop them from
      the batch, retry without them. Doesn't count against the 5xx retry budget.
    - 429: re-raise so the caller can bail.
    """
    from huggingface_hub import CommitOperationAdd
    from huggingface_hub.utils import HfHubHTTPError

    current_batch = list(batch)
    attempts = 0
    retries_5xx = 0
    redaction_totals: dict[str, int] = {}

    for _ in range(MAX_COMMIT_ITERATIONS):
        attempts += 1
        operations = []
        iter_redactions: dict[str, int] = {}
        for p in current_batch:
            redacted, counts = redact_secrets(p.read_bytes())
            for k, v in counts.items():
                iter_redactions[k] = iter_redactions.get(k, 0) + v
            operations.append(CommitOperationAdd(
                path_in_repo=p.relative_to(PROJECTS_DIR).as_posix(),
                path_or_fileobj=redacted,
            ))
        if not redaction_totals:
            redaction_totals = iter_redactions
        try:
            api.create_commit(
                repo_id=hf_repo,
                repo_type="dataset",
                operations=operations,
                commit_message=commit_message,
            )
            return attempts, current_batch, redaction_totals
        except HfHubHTTPError as e:
            status = e.response.status_code if e.response is not None else None
            body = e.response.text if e.response is not None else ""
            if status == 429:
                raise
            if status == 400 and "Offending files" in body:
                offenders = set(_OFFENDING_FILE_RE.findall(body))
                if not offenders:
                    log("400 mentioned 'Offending files' but no parseable paths; raising")
                    raise
                kept = [p for p in current_batch
                        if p.relative_to(PROJECTS_DIR).as_posix() not in offenders]
                dropped = len(current_batch) - len(kept)
                log(f"HF scanner flagged {dropped} file(s) past redaction; dropping and "
                    f"retrying with {len(kept)} files (likely new secret pattern, extend REDACTION_RULES)")
                if not kept:
                    return attempts, [], redaction_totals
                current_batch = kept
                continue
            if status is not None and 500 <= status < 600:
                retries_5xx += 1
                if retries_5xx >= MAX_BATCH_RETRIES:
                    raise
                backoff = INITIAL_BACKOFF_SECONDS * (2 ** (retries_5xx - 1))
                log(f"Commit got {status}, 5xx retry {retries_5xx}/{MAX_BATCH_RETRIES} in {backoff}s")
                time.sleep(backoff)
                continue
            raise
    raise RuntimeError(f"Batch failed after {MAX_COMMIT_ITERATIONS} iterations")


def load_manifest() -> dict[str, list[int]]:
    if not MANIFEST_FILE.exists():
        return {}
    try:
        return json.loads(MANIFEST_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        log("Manifest unreadable; rebuilding from remote sizes")
        return {}


def save_manifest(manifest: dict[str, list[int]]) -> None:
    MANIFEST_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = MANIFEST_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(manifest), encoding="utf-8")
    tmp.replace(MANIFEST_FILE)


def select_uploads(
    local: dict[str, tuple[Path, int, int]],
    remote: dict[str, int],
    manifest: dict[str, list[int]],
) -> tuple[list[str], list[str]]:
    """Return (new, changed) relpaths to upload; records verified-current files in manifest.

    local maps relpath -> (path, size, mtime_ns); remote maps relpath -> size.
    """
    new, changed = [], []
    for rel, (path, size, mtime) in local.items():
        if rel not in remote:
            new.append(rel)
            continue
        entry = manifest.get(rel)
        if entry is not None:
            if entry == [size, mtime]:
                continue
            if size < entry[0]:
                log(f"{rel} shrank since its last upload ({entry[0]} -> {size} bytes); not overwriting")
                continue
            changed.append(rel)
            continue
        if size == remote[rel]:
            manifest[rel] = [size, mtime]
            continue
        redacted_size = len(redact_secrets(path.read_bytes())[0])
        if redacted_size == remote[rel]:
            manifest[rel] = [size, mtime]
        elif redacted_size > remote[rel]:
            changed.append(rel)
        else:
            log(f"{rel} is smaller than its remote copy ({redacted_size} < {remote[rel]}); not overwriting")
    return new, changed


def do_backup(dry_run: bool = False) -> str:
    """Diff local vs remote and push missing or changed files in fixed-size batches.

    Bails on 429 (per-hour commit cap) and stops at MAX_ATTEMPTS_PER_RUN to leave
    buffer for the next run. Stamp file is written only when every missing file
    landed; partial runs leave it untouched so the next run picks up the rest.
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert PROJECTS_DIR.exists(), f"Projects dir missing: {PROJECTS_DIR}"

    from huggingface_hub import HfApi
    from huggingface_hub.utils import HfHubHTTPError

    api = HfApi()

    repo_name = resolve_repo_name()
    assert repo_name, "do_backup called without a repo name (main() should have skipped)"
    namespace = api.whoami()["name"]
    hf_repo = f"{namespace}/{repo_name}"

    log(f"Ensuring private repo {hf_repo}")
    api.create_repo(repo_id=hf_repo, repo_type="dataset", private=True, exist_ok=True)

    collection_slug = get_or_create_collection(api, namespace)
    log(f"Adding {hf_repo} to collection {collection_slug}")
    api.add_collection_item(
        collection_slug=collection_slug,
        item_id=hf_repo,
        item_type="dataset",
        exists_ok=True,
    )

    log("Listing remote files...")
    remote = {
        e.path: e.size
        for e in api.list_repo_tree(repo_id=hf_repo, repo_type="dataset", recursive=True)
        if e.path.endswith(BACKUP_SUFFIXES) and getattr(e, "size", None) is not None
    }
    log(f"Remote has {len(remote)} files")

    log(f"Scanning {PROJECTS_DIR}...")
    local: dict[str, tuple[Path, int, int]] = {}
    for p in sorted(PROJECTS_DIR.rglob("*")):
        if p.suffix in BACKUP_SUFFIXES and p.is_file():
            st = p.stat()
            local[p.relative_to(PROJECTS_DIR).as_posix()] = (p, st.st_size, st.st_mtime_ns)
    manifest = load_manifest()
    new, changed = select_uploads(local, remote, manifest)
    log(f"Local has {len(local)} files; {len(new)} missing on remote, {len(changed)} changed since upload")
    if dry_run:
        mb = lambda rels: sum(local[r][1] for r in rels) / 1e6
        return (f"Dry run: would upload {len(new)} new ({mb(new):.1f} MB) and "
                f"{len(changed)} changed ({mb(changed):.1f} MB); manifest not written")
    save_manifest(manifest)
    missing = [local[rel][0] for rel in new + changed]
    stats_at_selection = {local[rel][0]: [local[rel][1], local[rel][2]] for rel in new + changed}

    if not missing:
        STAMP_FILE.parent.mkdir(parents=True, exist_ok=True)
        STAMP_FILE.write_text(json.dumps({"last_backup_date": today}), encoding="utf-8")
        return f"Up to date ({len(local)} files, 0 to upload)"

    n_batches = (len(missing) + BATCH_SIZE - 1) // BATCH_SIZE
    attempts_used = 0
    files_uploaded = 0
    files_skipped_by_scanner = 0
    redaction_grand_total: dict[str, int] = {}
    bailed_429 = False
    completed_all_batches = True

    for i in range(0, len(missing), BATCH_SIZE):
        if attempts_used >= MAX_ATTEMPTS_PER_RUN:
            log(f"Per-run attempt budget reached ({attempts_used}/{MAX_ATTEMPTS_PER_RUN}); pausing")
            completed_all_batches = False
            break
        batch = missing[i:i + BATCH_SIZE]
        batch_idx = i // BATCH_SIZE + 1
        msg = f"backup {today} batch {batch_idx}/{n_batches} ({len(batch)} files)"
        try:
            n_attempts, committed, redactions = _commit_batch(api, hf_repo, batch, msg)
        except HfHubHTTPError as e:
            if e.response is not None and e.response.status_code == 429:
                log(f"429 rate limited on batch {batch_idx}: bailing, next run will resume")
                bailed_429 = True
                completed_all_batches = False
                break
            raise
        attempts_used += n_attempts
        files_uploaded += len(committed)
        for p in committed:
            manifest[p.relative_to(PROJECTS_DIR).as_posix()] = stats_at_selection[p]
        save_manifest(manifest)
        files_skipped_by_scanner += len(batch) - len(committed)
        for k, v in redactions.items():
            redaction_grand_total[k] = redaction_grand_total.get(k, 0) + v
        redact_summary = ", ".join(f"{k}:{v}" for k, v in redactions.items()) or "none"
        log(
            f"Batch {batch_idx}/{n_batches}: committed {len(committed)}/{len(batch)} files "
            f"in {n_attempts} attempt(s); redactions [{redact_summary}]; "
            f"uploaded {files_uploaded}/{len(missing)}, attempts {attempts_used}/{MAX_ATTEMPTS_PER_RUN}"
        )

    # Stamp if we processed every batch (even with some scanner skips), so the next
    # run picks up new transcripts rather than re-attempting the same flagged files.
    if completed_all_batches:
        STAMP_FILE.parent.mkdir(parents=True, exist_ok=True)
        STAMP_FILE.write_text(json.dumps({"last_backup_date": today}), encoding="utf-8")
        log("Stamp file written")
    grand_redact = ", ".join(f"{k}:{v}" for k, v in redaction_grand_total.items()) or "none"
    suffix = (
        " (rate-limited)" if bailed_429
        else "" if completed_all_batches
        else " (paused, will resume)"
    )
    return (
        f"Uploaded {files_uploaded}/{len(missing)} files ({len(new)} new, {len(changed)} changed), "
        f"{files_skipped_by_scanner} skipped by scanner, "
        f"{attempts_used} commit attempts; redactions [{grand_redact}]{suffix}"
    )


def main():
    """Sleep 5 min so short sessions skip the hook, then upload if today is unstamped."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--now",
        action="store_true",
        help="Skip the 5-minute warm-up sleep and start uploading immediately. Intended for manual flushes.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be uploaded (implies --now --force); upload nothing.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run even if today's backup is already stamped.",
    )
    args = parser.parse_args()
    if args.dry_run:
        args.now = args.force = True

    if not BACKUP_ENABLED or BACKUP_DISABLED:
        print(json.dumps({}))
        return

    if not resolve_repo_name():
        print(json.dumps({}))
        log("no backup repo name (CLAUDE_CODE_BACKUP_REPO_NAME / environment.json backup_repo_name) — backup skipped")
        return

    if not needs_backup() and not args.force:
        print(json.dumps({}))
        return

    print(json.dumps({}))

    lock = acquire_lock()
    if lock is None:
        log(f"Another backup process holds {LOCK_FILE}, skipping")
        return

    if args.now:
        log("--now: skipping warm-up sleep")
    else:
        log("Waiting 5 minutes before backup...")
        time.sleep(300)
        if not needs_backup() and not args.force:
            log("Stamp file appeared during wait, skipping")
            return
    start_watchdog(MAX_BACKUP_SECONDS)
    msg = do_backup(dry_run=args.dry_run)
    log(f"Backup: {msg}")


if __name__ == "__main__":
    main()
