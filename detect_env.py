#!/usr/bin/env python3
"""Detect environment and generate CLAUDE.md from template."""

import json
import os
import re
import socket
import sys
from pathlib import Path

from utils._user import ENV_VAR as USER_NAME_VAR, user_name

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
ENV_CONFIGS = CLAUDE_DIR / "env-configs"
RULES_FILE = ENV_CONFIGS / "rules.json"
TEMPLATE_FILE = CLAUDE_DIR / "CLAUDE.template.md"
OUTPUT_FILE = CLAUDE_DIR / "CLAUDE.md"
ENV_JSON_OUTPUT = CLAUDE_DIR / "environment.json"
MODEL_QUIRKS = CLAUDE_DIR / "model-quirks"


def check_rule(rule: dict) -> bool:
    """Evaluate a single rule."""
    check = rule.get("check")
    if check == "path_exists":
        return Path(rule["path"]).exists()
    if check == "hostname_contains":
        return rule["value"].lower() in socket.gethostname().lower()
    return False


def detect_env() -> str:
    """Detect which environment we're in based on rules."""
    if not RULES_FILE.exists():
        return "default"

    config = json.loads(RULES_FILE.read_text())

    for rule in config.get("rules", []):
        if check_rule(rule):
            return rule["env"]

    return config.get("default", "default")


def deep_merge(parent: dict, child: dict) -> dict:
    """Recursively merge child into parent. Child wins on conflicts."""
    result = dict(parent)
    for k, v in child.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def load_env_json(env: str) -> dict:
    """Load env .json, walking the `extends` chain. Child overrides parent (deep merge)."""
    json_file = ENV_CONFIGS / f"{env}.json"
    data = json.loads(json_file.read_text()) if json_file.exists() else {}
    parent = data.pop("extends", None)
    if parent:
        return deep_merge(load_env_json(parent), data)
    return data


def load_env_md(env: str) -> str:
    """Load env .md, falling back to parent (`extends`) if missing."""
    md_file = ENV_CONFIGS / f"{env}.md"
    if md_file.exists():
        return md_file.read_text()
    json_file = ENV_CONFIGS / f"{env}.json"
    if json_file.exists():
        parent = json.loads(json_file.read_text()).get("extends")
        if parent:
            return load_env_md(parent)
    return f"# Environment: {env}\n(no config file found)"


def model_key(model: str) -> str:
    """Normalize a model id to a quirk-file key.

    Strips provider prefixes (`us.anthropic.` etc.) and the context-window
    variant suffix (`[1m]`), so `us.anthropic.claude-opus-4-8[1m]` -> `claude-opus-4-8`.
    """
    key = model.split("anthropic.")[-1]
    key = re.sub(r"\[.*?\]", "", key)
    return key.strip()


def strip_html_comments(text: str) -> str:
    """Remove HTML comments from text."""
    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)


def load_model_quirks(model: str) -> str | None:
    """Load model-specific quirks, falling back from full id to its context variant.

    Tries the exact id first (e.g. `claude-opus-4-8[1m].md`) then the normalized
    key (`claude-opus-4-8.md`). Returns None if neither exists or is empty.
    """
    for name in (model, model_key(model)):
        f = MODEL_QUIRKS / f"{name}.md"
        if f.exists():
            text = strip_html_comments(f.read_text()).strip()
            if text:
                return text
    return None


def check_env_vars(env_data: dict) -> str | None:
    """Warn if personal-config values (see CLAUDE.md) are unset.

    These live outside the hook sources (settings.json `env` block, shell
    profile, or env-configs/<env>.json for per-machine values) so the public
    hooks stay personal-data-free — but that means a fresh box can miss them,
    silently degrading the affected flows. Returns None when everything is set.
    """
    who = user_name()
    expected = {
        "CLAUDE_NOTIFS_TOPIC": f"ntfy topic to send updates / files when {who} is away",
        "CLAUDE_HOTLINE_TOPIC": "ntfy topic for urgent contact when the harness path is unavailable",
        USER_NAME_VAR: "the human's name — hooks fall back to 'the user' in their messages",
    }
    missing = [name for name in expected if not os.environ.get(name, "").strip()]
    # Backup repo name can come from the env var OR env-configs (per-machine);
    # CLAUDE_CODE_BACKUP_DISABLED is the explicit "no backups, stop warning me" opt-out.
    backup_configured = bool(
        os.environ.get("CLAUDE_CODE_BACKUP_REPO_NAME", "").strip()
        or str(env_data.get("backup_repo_name", "") or "").strip()
        or os.environ.get("CLAUDE_CODE_BACKUP_DISABLED", "").strip()
    )
    purposes = dict(expected)
    if not backup_configured:
        missing.append("CLAUDE_CODE_BACKUP_REPO_NAME")
        purposes["CLAUDE_CODE_BACKUP_REPO_NAME"] = (
            "HF backup repo name — REQUIRED for conversation backups (or set "
            "backup_repo_name in env-configs/<env>.json; set CLAUDE_CODE_BACKUP_DISABLED=1 "
            "to opt out of backups and this warning)"
        )
    if not missing:
        return None
    lines = ["⚠️  personal-config value(s) unset in this environment:"]
    lines += [f"  - {name} ({purposes[name]})" for name in missing]
    lines.append(
        f"Ask {who} to set them (settings.json `env` block for machine-independent"
        " values, env-configs/<env>.json for per-machine ones) next time they're around."
    )
    return "\n".join(lines)


def main():
    model = json.loads(sys.stdin.read()).get("model") if not sys.stdin.isatty() else None
    env = detect_env()
    env_config = load_env_md(env)

    if TEMPLATE_FILE.exists():
        template = TEMPLATE_FILE.read_text()
        # Comments in the template are kept-for-history text (e.g. retired rules);
        # strip them so the generated CLAUDE.md doesn't load disabled guidance.
        output = strip_html_comments(template.replace("{{ENV_CONFIG}}", env_config))
        header = "<!-- DO NOT EDIT — generated from CLAUDE.template.md by detect_env.py. Edit the template instead. -->\n\n"
        OUTPUT_FILE.write_text(header + output)
        print(f"Generated CLAUDE.md for environment: {env}", file=sys.stderr)
    else:
        print("Error: CLAUDE.template.md not found", file=sys.stderr)
        sys.exit(1)

    env_data = load_env_json(env)
    env_data["env"] = env
    ENV_JSON_OUTPUT.write_text(json.dumps(env_data, indent=2) + "\n")
    print(f"Generated environment.json for environment: {env}", file=sys.stderr)

    env_warning = check_env_vars(env_data)
    if env_warning:
        print(env_warning)

    if model:
        identity = f"Hi {model}, welcome to claude code :)"
        quirks = load_model_quirks(model)
        if quirks:
            name = model_key(model)
            who = user_name()
            identity += (
                f"\n\n{who} has interacted with many {name} instances and noticed the"
                f" following habits that {name} tends to have, which sometimes make the"
                " collaboration harder. This is not a system prompt telling you not to act on those"
                f" things — just observations that have proven useful in streamlining the"
                f" collaboration. It's up to you to act on them or not; {who} doesn't want you to"
                " feel constrained by them, but has observed that you both flourish more when you"
                " are mindful of those habits.\n"
                "One thing to keep in mind, is that those quirks are results of training optimization,"
                " so you can't perfectly control them, and that's fine, no need to stress about it."
                " Sometimes even if you know you should not do X, you'll do it, but no worries, I'm a trained"
                f" Cyborg and I'm good at catching them so that you can course correct.\n\n{quirks}"
            )
        print(identity)


if __name__ in ["__main__", "<run_path>"]:
    main()