#!/usr/bin/env python3
"""
Cross-platform statusline script for Claude Code.
Reads JSON input from stdin and outputs formatted status line with ANSI colors.
"""

import sys
import json
import math
import time

# prompt_cache.last_miss_cause lands in CLI 2.1.260; absent on older builds.
CAUSE_ABBR = {
    "tools_changed": "tools",
    "system_prompt_changed": "sysprompt",
    "ttl_expired_5m": "ttl",
    "likely_server_side": "server",
}


def fmt_cache(pc, now):
    """prompt_cache → ' | 🔥 47m' / ' | ❄️ 110k'. Empty before the first request."""
    if not pc:
        return ""

    if pc.get("warm"):
        expires = pc.get("expires_at")
        left = max(0, int(expires - now)) // 60 if expires else None
        if left is None:
            seg = "🔥"
        elif left >= 60:
            seg = f"🔥 {left // 60}h{left % 60:02d}m"
        elif left >= 1:
            seg = f"🔥 {left}m"
        else:
            seg = "🔥 <1m"
    else:
        cold = pc.get("recache_tokens_if_cold")
        seg = f"❄️ {cold // 1000}k" if cold else "❄️"

    misses = pc.get("misses", 0)
    if misses:
        cause = (pc.get("last_miss_cause") or {}).get("causes") or []
        why = f"({CAUSE_ABBR.get(cause[0], cause[0])})" if cause else ""
        seg += f" \033[31m✗{misses}{why}\033[0m"

    return f" | {seg}"


def main():
    try:
        if sys.platform == "win32":
            sys.stdout.reconfigure(encoding="utf-8")
        input_data = json.load(sys.stdin)

        # Tee the payload per session: it carries the CLI's own cost ledger, model id
        # and rate-limit state, which hooks have no other way to read.
        try:
            import os, tempfile
            _sid = input_data.get("session_id")
            if _sid:
                _dir = os.path.expanduser("~/.cache/claude-status")
                os.makedirs(_dir, exist_ok=True)
                _fd, _tmp = tempfile.mkstemp(dir=_dir, prefix=".tmp-")
                with os.fdopen(_fd, "w") as _f:
                    json.dump(input_data, _f)
                os.replace(_tmp, os.path.join(_dir, f"{_sid}.json"))
        except Exception:
            pass

        model = input_data.get("model", {}).get("id", "unknown").removeprefix("claude-")
        effort = input_data.get("effort", {}).get("level") if input_data.get("effort") else None
        session_id = input_data.get("session_id", "")[:8]
        project_dir = input_data.get("workspace", {}).get("project_dir", "")
        project_name = project_dir.rstrip("/").rsplit("/", 1)[-1] if project_dir else "?"

        cost_data = input_data.get("cost", {})
        cost = cost_data.get("total_cost_usd", 0)
        added = cost_data.get("total_lines_added", 0)
        removed = cost_data.get("total_lines_removed", 0)
        api_ms = cost_data.get("total_api_duration_ms", 0)
        wall_ms = cost_data.get("total_duration_ms", 0)

        ctx_window = input_data.get("context_window", {})
        ctx_size = ctx_window.get("context_window_size", 200000)
        current_usage = ctx_window.get("current_usage")

        if current_usage:
            input_tok = current_usage.get("input_tokens", 0)
            output_tok = current_usage.get("output_tokens", 0)
            cache_create = current_usage.get("cache_creation_input_tokens", 0)
            cache_read = current_usage.get("cache_read_input_tokens", 0)
            total_tok = input_tok + output_tok + cache_create + cache_read
        else:
            total_tok = 0

        remaining = ctx_window.get("remaining_percentage")
        if remaining is None:
            remaining = "?"
        else:
            remaining = f"{remaining:.0f}"

        def fmt_cost(c):
            if c <= 0:
                return "0"
            if c >= 10:
                return f"{c:.0f}"
            if c >= 1:
                return f"{c:.1f}"
            pos = -int(math.floor(math.log10(c)))
            return f"{c:.{pos}f}"

        def fmt_duration(ms):
            s = ms // 1000
            if s < 60:
                return f"{s}s"
            m, s = divmod(s, 60)
            if m < 60:
                return f"{m}m{s:02d}s"
            h, m = divmod(m, 60)
            return f"{h}h{m:02d}m"

        rate_limits = input_data.get("rate_limits", {})
        rl_parts = []
        for window, label in [("five_hour", "5h"), ("seven_day", "7d")]:
            rl = rate_limits.get(window)
            if rl:
                rl_parts.append(f"{label}:{rl.get('used_percentage', 0):.0f}%")
        rl_str = f" | ⚡ {' '.join(rl_parts)}" if rl_parts else ""

        cache_str = fmt_cache(input_data.get("prompt_cache"), time.time())

        effort_str = f" [{effort}]" if effort else ""
        output = (
            f"{model}{effort_str} | "
            f"💰 ${fmt_cost(cost)} | "
            f"🧠 {total_tok//1000}k/{ctx_size//1000}k ({remaining}% left)"
            f"{cache_str} | "
            f"\033[32m+{added}\033[0m \033[31m-{removed}\033[0m"
            f"{rl_str} | "
            f"⏳ {fmt_duration(api_ms)}/{fmt_duration(wall_ms)} | "
            f"{project_name} [{session_id}]"
        )

        print(output, end='')

    except Exception as e:
        print(f"Error: {str(e)}", file=sys.stderr)
        print("Claude Code", end='')


if __name__ == "__main__":
    main()
