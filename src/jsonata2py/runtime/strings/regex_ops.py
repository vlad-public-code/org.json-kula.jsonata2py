"""Regex support for JSONata regex literals and $match/$replace/$split.

Ported from org.json_kula.jsonata_jvm.runtime.RegexRegistry and
runtime.string.RegexOps.

Uses the PyPI `regex` module (not stdlib re) -- see docs/porting-design-spec.md
D4 for why: JSONata's regex dialect needs \\p{...} Unicode properties,
atomic groups, possessive quantifiers, and variable-length lookbehind, none
of which stdlib re supports uniformly.

The byte-offset conversion layer Java needs (joni operates on UTF-8 bytes)
is deleted per D4: `regex` reports character offsets against the str
directly, which is what JSONata's $match(...).index is specified to be.
"""

from __future__ import annotations

import threading
from itertools import islice

import regex as _regex_mod

from ..values import JRegex

# Compiled regex literals are cached process-wide. The cache MUST be bounded:
# $eval makes expression text -- and therefore regex literals -- dynamic, so a
# long-lived service compiling user-supplied expressions would otherwise grow
# without limit. This mirrors the bounded factory caches (_COMPILE_CACHE_LIMIT,
# _CODE_CACHE_MAX_BYTES), which exist for exactly the same reason.
_REGEX_CACHE_LIMIT = 512
# Evicted in batches so eviction cost is amortised rather than paid per insert.
_REGEX_CACHE_EVICT = 128

_CACHE: dict[str, _regex_mod.Pattern] = {}
# Guards writers only. Readers use a bare dict.get() (atomic), so the hit path
# stays lock-free. Eviction is insertion-order FIFO rather than true LRU on
# purpose: an LRU reorder would have to happen on the *hit* path, which would
# either need this lock on every lookup or race exactly the way the unlocked
# $eval cache did (a concurrent evicting insert making move_to_end() raise).
_CACHE_LOCK = threading.Lock()


def _compile(pattern: str, flags: str) -> _regex_mod.Pattern:
    opts = 0
    if "i" in flags:
        opts |= _regex_mod.IGNORECASE
    if "m" in flags:
        opts |= _regex_mod.MULTILINE
    translated = pattern_translate(pattern)
    return _regex_mod.compile(translated, opts)


def pattern_translate(pattern: str) -> str:
    """Thin syntax shim for the JSONata/ECMAScript regex dialect gap that
    the `regex` module doesn't already cover for free. \\d/\\w/\\s default
    to Unicode in both `regex` and JSONata, so no translation is needed
    there. (?<name>...) -> (?P<name>...) is handled automatically by the
    `regex` module itself, so this is currently a passthrough; extend as
    dialect gaps are found against the test suite in Phase 5b."""
    return pattern


def regex_value(pattern: str, flags: str) -> JRegex:
    """Compiles a JSONata regex literal (caching the compilation) and
    returns it as a JRegex value."""
    key = f"{pattern}\0{flags}"
    compiled = _CACHE.get(key)
    if compiled is None:
        compiled = _compile(pattern, flags)
        with _CACHE_LOCK:
            if len(_CACHE) >= _REGEX_CACHE_LIMIT:
                # Safe to iterate: every writer holds the lock, and readers
                # only ever call .get(), which does not mutate.
                for old in list(islice(_CACHE, _REGEX_CACHE_EVICT)):
                    _CACHE.pop(old, None)
            _CACHE[key] = compiled
    return JRegex(pattern, flags, compiled)


def compiled_of(value: JRegex) -> _regex_mod.Pattern:
    if value.compiled is not None:
        return value.compiled
    return _compile(value.pattern, value.flags)


def test_match(value: JRegex, text: str) -> bool:
    """~> with a regex on the right: test whether text matches."""
    return compiled_of(value).search(text) is not None


def build_literal_regex(s: str) -> _regex_mod.Pattern:
    """Builds a pattern that matches the literal string s (no special
    regex chars)."""
    return _regex_mod.compile(_regex_mod.escape(s))
