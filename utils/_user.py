"""Shared helper: resolve the human's name from the I_LOVE_BEING_A_USER env var.

Hooks that talk about the human in Claude-facing messages import this instead
of hardcoding a name, so the hook sources stay personal-data-free. Set
I_LOVE_BEING_A_USER in your shell profile (e.g. `export I_LOVE_BEING_A_USER="Ada"`);
detect_env.py warns at session start when it's unset.

Not a hook itself — imported by detect_env.py, teammate_guard.py, etc.
"""

import os
import unicodedata

ENV_VAR = "I_LOVE_BEING_A_USER"


def user_name(default: str = "the user") -> str:
    """The human's name, or `default` when the env var is unset/blank."""
    return os.environ.get(ENV_VAR, "").strip() or default


def user_name_ascii_upper(default: str = "USER") -> str:
    """Accent-stripped, uppercased, dash-joined name for use in bypass tokens.

    "Clément" -> "CLEMENT"; unset -> `default`. Keeps tokens grep-safe ASCII.
    """
    name = user_name(default)
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    ascii_name = ascii_name or default
    return "-".join(ascii_name.upper().split())
