"""Ask whether a text patch from the claude-code-patches repo is in effect.

Hooks sometimes need to know how the `claude` binary they are running under was
patched, because a patch can remove the very problem the hook works around (see
`force_background_bash.py`) or make its advice wrong (see `bgwatch_hint.py`).

Two things have to be true for a patch to be in effect, and checking only the
first is the trap this module exists to avoid: the patched bytes must be present
AND the module holding them must run from source. Since 2.1.250 (Bun 1.4.1)
every module also ships pre-compiled JSC bytecode that the loader runs without
checking the text still matches, so patched text whose bytecode is still enabled
is inert — `grep` finds it and the process executes stock code anyway. The
patches repo's `zz-bytecode-off.py` zeroes the bytecode length for every module
it sees changed; `bun_module_bytecode_len` is how we confirm it did.

`text_patch_state` is deliberately TRI-state. A detector that answers only
yes/no cannot distinguish "the binary is unpatched" from "my anchor moved and I
can no longer look", and silently reports the second as the first — which is how
this file's ancestor read every patched binary as unpatched for a month after
2.1.270 deleted the string it anchored on. Callers that can afford to say
something about `unknown` should; callers that can't should at least not treat
it as a confident "unpatched".
"""

from __future__ import annotations

import json
import os
import re
import shutil
import struct
import tempfile

# Bun single-file executable layout. `_TRAILER` ends the image; 24 bytes before
# it sit the module-table offset and length; records are `_REC` bytes of
# (name off/len, code off/len, sourcemap off/len, bytecode off/len) as u32.
_TRAILER = b"\n---- Bun! ----\n"
_REC = 52
# Module names are paths in Bun's embedded virtual filesystem, whose root is
# platform-dependent — hard-coding the POSIX one made every base candidate look
# wrong on Windows, so the graph never parsed and every patched binary read as
# unpatched. Kept in sync with NAME_PREFIXES in the patches repo's _bungraph.py.
_NAME_PREFIXES = (b"/$bunfs/", b"B:/~BUN/", b"B:\\~BUN\\")

_SHIM_SUFFIXES = (".cmd", ".bat", ".ps1")
_SHIM_EXE_RE = re.compile(r'"?([a-zA-Z]:\\[^"\r\n]*?claude\.exe)"?')

# Generic override first, then the name `force_background_bash.py` shipped with
# (its tests point it at a missing file to get stock rules).
_BIN_ENV_VARS = ("CLAUDE_HOOKS_CLAUDE_BIN", "FORCE_BACKGROUND_BASH_CLAUDE_BIN")

PATCHED = "patched"
STOCK = "stock"
UNKNOWN = "unknown"


def bun_module_bytecode_len(data, off):
    """Length of the JSC bytecode blob for the Bun standalone module whose JS
    text covers file offset `off` (0 = that module runs from source). None if the
    graph can't be parsed. Mirrors `_bungraph.py` in the patches repo."""
    t = data.rfind(_TRAILER)
    if t == -1:
        return None
    mp_off, mp_len = struct.unpack_from("<II", data, t - 24)
    if mp_len == 0 or mp_len % _REC:
        return None
    p = (t // 512) * 512
    base = None
    while p >= 0:
        (count,) = struct.unpack_from("<Q", data, p)
        if 0 <= (t + len(_TRAILER)) - (p + 8) - count < 65536:
            tbl = p + 8 + mp_off
            if tbl + mp_len <= len(data):
                noff, nlen = struct.unpack_from("<II", data, tbl)
                name = data[p + 8 + noff : p + 8 + noff + 8]
                if 0 < nlen < 512 and name.startswith(_NAME_PREFIXES):
                    base = p + 8
                    break
        p -= 512
    if base is None:
        return None
    tbl = base + mp_off
    for i in range(mp_len // _REC):
        r = tbl + i * _REC
        _n, _nl, coff, clen, _s, _sl, _b, blen = struct.unpack_from("<8I", data, r)
        if base + coff <= off < base + coff + clen:
            return blen
    return None


def module_runs_from_source(data, off):
    """True iff the module covering `off` has its bytecode disabled, i.e. a text
    edit there actually executes."""
    return bun_module_bytecode_len(data, off) == 0


def claude_binary():
    """Path of the live claude bundle, or None.

    `which claude` can land on a Windows launcher shim — a one-line .cmd that
    execs the real .exe — which carries no Bun module graph at all, so scanning
    it makes a patched install read as unpatched. Dereference the shim, then
    fall back to the native-installer location the way the patches repo's
    `candidate_binaries()` does.
    """
    for var in _BIN_ENV_VARS:
        override = os.environ.get(var)
        if override:
            return override
    which = shutil.which("claude")
    if which:
        if os.path.splitext(which)[1].lower() in _SHIM_SUFFIXES:
            with open(which, encoding="utf-8", errors="replace") as f:
                m = _SHIM_EXE_RE.search(f.read())
            if m and os.path.isfile(m.group(1)):
                return m.group(1)
        else:
            return which
    for cand in (
        os.path.expanduser("~/.local/bin/claude.exe"),
        os.path.expanduser("~/.local/bin/claude"),
    ):
        if os.path.isfile(cand):
            return cand
    return None


def inspect_cached(cache_name, inspect):
    """Run `inspect(data)` over the live bundle's bytes, memoised per binary.

    The cache is keyed on (realpath, size, mtime), so a claude update — which
    ships a fresh unpatched binary — invalidates it on its own. One file per
    detector rather than one shared dict: writers then never read-modify-write
    the same file, so concurrent hooks can't clobber each other's entries.
    Returns None if the binary can't be located or read.
    """
    path = claude_binary()
    if not path:
        return None
    try:
        path = os.path.realpath(path)
        st = os.stat(path)
    except OSError:
        return None
    key = [path, st.st_size, int(st.st_mtime)]
    # Unix /tmp is shared, so the cache name carries the uid to keep users apart.
    # Windows has no os.getuid() and hands each user their own temp dir anyway.
    uid = os.getuid() if hasattr(os, "getuid") else ""
    cache = os.path.join(tempfile.gettempdir(), f"clipatch_{cache_name}_{uid}.json")
    try:
        with open(cache, encoding="utf-8") as f:
            c = json.load(f)
        if c.get("key") == key:
            return c.get("value")
    except (OSError, ValueError):
        pass
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    value = inspect(data)
    try:
        tmp = cache + f".{os.getpid()}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"key": key, "value": value}, f)
        os.replace(tmp, cache)
    except OSError:
        pass
    return value


def text_patch_state(stock, patched, cache_name):
    """Whether a same-length text patch is in effect, as PATCHED / STOCK / UNKNOWN.

    `stock` and `patched` are the exact byte strings the patch script swaps
    between — pass the patch's own PATTERN and PATCHED constants so that a patch
    which still applies cannot disagree with a detector that no longer finds it.

    UNKNOWN means neither string is in the binary: upstream moved the code out
    from under both the patch and this check. It is NOT the same answer as
    STOCK, and callers should not collapse it into one. Patched text whose
    module still runs from bytecode is reported STOCK, because that is what the
    process actually executes.
    """

    def inspect(data):
        at = data.find(patched)
        if at != -1:
            return PATCHED if module_runs_from_source(data, at) else STOCK
        return STOCK if stock in data else UNKNOWN

    return inspect_cached(cache_name, inspect) or UNKNOWN
