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
from collections.abc import Iterator
from itertools import islice
from typing import Any

import regex as _regex_mod

from ...errors import _RuntimeEvaluationError as RuntimeEvaluationError
from ..values import MISSING, JLambda, JRegex

# Compiled regex literals are cached process-wide. The cache MUST be bounded:
# $eval makes expression text -- and therefore regex literals -- dynamic, so a
# long-lived service compiling user-supplied expressions would otherwise grow
# without limit. This mirrors the bounded factory caches (_COMPILE_CACHE_LIMIT,
# _CODE_CACHE_MAX_BYTES), which exist for exactly the same reason.
_REGEX_CACHE_LIMIT = 512
# Evicted in batches so eviction cost is amortised rather than paid per insert.
_REGEX_CACHE_EVICT = 128
# ...and the cache MUST also be bounded in BYTES, not just entries. The key is
# the pattern text, and a pattern can come straight out of an input document
# ($match(s, $eval("/" & doc.pat & "/"))), so its length is attacker
# controlled: 512 entries of an arbitrarily long pattern -- plus the compiled
# Pattern, whose size scales with the pattern too -- is an unbounded-memory
# path from a single hostile document. An over-long pattern is therefore
# compiled but never retained: O(1) memory, at the cost of recompiling that
# one pattern on every evaluation.
#
# Measured on the ORIGINAL pattern text, never the translated one:
# pattern_translate expands each `.` to a 20-character class, so guarding on
# the output would make the limit depend on how many metacharacters the
# rewrite happened to hit instead of on what the user actually wrote.
_MAX_CACHEABLE_PATTERN = 1024  # characters

_CACHE: dict[str, _regex_mod.Pattern] = {}
# Guards writers only. Readers use a bare dict.get() (atomic), so the hit path
# stays lock-free. Eviction is insertion-order FIFO rather than true LRU on
# purpose: an LRU reorder would have to happen on the *hit* path, which would
# either need this lock on every lookup or race exactly the way the unlocked
# $eval cache did (a concurrent evicting insert making move_to_end() raise).
_CACHE_LOCK = threading.Lock()


def _compile(pattern: str, flags: str) -> _regex_mod.Pattern:
    # The lexer only ever produces `i` and `m` (parser/lexer.py:273-279):
    # `/x/s` is an S0202 at parse time, in this port and in the reference
    # alike, so `.` is always non-dotAll here and needs no dotall branch.
    opts = _regex_mod.IGNORECASE if "i" in flags else 0
    # No MULTILINE: pattern_translate rewrites every `^` and `$` it applies
    # to, so re.MULTILINE would be dead at best and a second, WRONGER
    # line-break definition at worst.
    return _regex_mod.compile(pattern_translate(pattern, "m" in flags), opts)


# ECMAScript's LineTerminator set (ES2024 11.3): LF, CR, LS, PS. Python's
# re.MULTILINE breaks lines on `\n` ONLY, and Python's `.` excludes `\n`
# ONLY, so both have to be spelled out explicitly.
_ECMA_LINE_TERMINATORS = r"[\n\r\u2028\u2029]"
# `.` without /s: any code point that is not a LineTerminator. Python's `.`
# also matches \r, U+2028 and U+2029, so $contains("a\rb", /a.b/) was true
# here and false in the reference.
_ECMA_DOT = r"[^\n\r\u2028\u2029]"
# `^` with /m: start of input, or immediately after a LineTerminator.
_ECMA_CARET_MULTILINE = r"(?:\A|(?<=[\n\r\u2028\u2029]))"
# `$` with /m: end of input, or immediately before a LineTerminator. Note
# CRLF counts as TWO terminators in ECMAScript, so "a\r\nb" has an empty
# line between them -- which is exactly what the lookaround pair yields.
_ECMA_DOLLAR_MULTILINE = r"(?:\Z|(?=[\n\r\u2028\u2029]))"
# `$` without /m: the very end of the input. Python's `$` (like Perl's)
# also matches just BEFORE a single trailing newline, so /$/ against "a\n"
# is index 2 in JavaScript but index 1 in Python, and /a$/ matches "a\n"
# in Python but not in JavaScript.
_ECMA_DOLLAR = r"\Z"


def pattern_translate(pattern: str, multiline: bool = False) -> str:
    """Rewrites a JSONata (i.e. ECMAScript) regex into the `regex` module's
    dialect. \\d/\\w/\\s default to Unicode in both, and
    (?<name>...) -> (?P<name>...) is handled by the `regex` module itself;
    what is left is the three constructs whose *meaning* differs, all of
    them disagreeing about which characters end a line:

      `.`  Python's matches \\r, U+2028 and U+2029; ECMAScript's does not.
      `$`  see _ECMA_DOLLAR / _ECMA_DOLLAR_MULTILINE.
      `^`  under /m, Python breaks on \\n only.

    A metacharacter is rewritten only where it IS a metacharacter: not
    backslash-escaped, not inside a character class, and not inside a
    \\p{...}/\\P{...} property name (where `^` is the `regex` module's
    property negation, e.g. \\p{^Lu}). ECMAScript has no other context in
    which an unescaped `^`, `$` or `.` is a literal.

    Runs once per distinct literal -- `regex_value` caches the compiled
    result -- so the per-character scan is paid at compile time only.
    Returns `pattern` itself when nothing needed rewriting.
    """
    out: list[str] = []
    start = 0  # start of the pending run of verbatim characters
    i = 0
    n = len(pattern)
    in_class = False
    while i < n:
        c = pattern[i]
        if c == "\\":
            # Skip the escape and whatever it escapes as one unit, so a
            # `\$` (or a `\\` before a `$`) is never mis-read.
            if pattern[i + 1 : i + 2] in ("p", "P") and pattern[i + 2 : i + 3] == "{":
                close = pattern.find("}", i + 3)
                i = n if close < 0 else close + 1
            else:
                i += 2
            continue
        if in_class:
            if c == "]":
                in_class = False
            i += 1
            continue
        if c == "[":
            in_class = True
            i += 1
            continue
        if c == "$":
            repl = _ECMA_DOLLAR_MULTILINE if multiline else _ECMA_DOLLAR
        elif c == ".":
            repl = _ECMA_DOT
        elif c == "^" and multiline:
            repl = _ECMA_CARET_MULTILINE
        else:
            i += 1
            continue
        out.append(pattern[start:i])
        out.append(repl)
        i += 1
        start = i
    if not out:
        return pattern
    out.append(pattern[start:])
    return "".join(out)


def regex_value(pattern: str, flags: str) -> JRegex:
    """Compiles a JSONata regex literal (caching the compilation) and
    returns it as a JRegex value.

    An over-long pattern is compiled but not cached (see
    _MAX_CACHEABLE_PATTERN). Only the caching changes -- the returned
    JRegex, and everything downstream of it, is identical either side of
    the limit. The check comes before the key is built so a hostile
    pattern never even materialises a second copy of itself.
    """
    if len(pattern) > _MAX_CACHEABLE_PATTERN:
        return JRegex(pattern, flags, _compile(pattern, flags))
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


def iter_matches(compiled: _regex_mod.Pattern, s: str) -> Iterator[_regex_mod.Match]:
    """Yields the successive matches of `compiled` in `s` using JSONata's
    matcher-closure cursor semantics.

    Ported from jsonata2js src/runtime/regex.js:76-100 -- `matchFrom` and
    the `next()` it installs, which is jsonata's own `evaluateRegex`
    closure. The cursor after a match sits at `m.end()` (that is where
    `RegExp#exec` with the /g flag leaves `lastIndex`), so a ZERO-LENGTH
    match does not advance it. `next()` therefore

      (a) stops as soon as the cursor has reached the end of the subject
          (`if (re.lastIndex >= str.length) return undefined`), and
      (b) raises D1004 when the match it just produced is zero length,
          because re-searching from an unadvanced cursor would repeat that
          match forever.

    The guard is on a *subsequent* match being empty -- never on the first
    one, and never on the pattern text. That is why `$match("abc", /$/)`
    is legal (one empty match at index 3, then the cursor is already at
    the end) while `$match("abc", /^/)` raises D1004 (empty match at 0,
    cursor still 0, so the next search repeats it).

    Callers MUST pull the next item BEFORE re-checking their own `limit`,
    the way regex.js does (`matches = matches.next(); count++`): a limit
    does not suppress the D1004 that advancing raises.
    """
    n = len(s)
    m = compiled.search(s, 0)
    if m is None:
        return
    yield m
    cursor = m.end()
    while cursor < n:
        m = compiled.search(s, cursor)
        if m is None:
            return
        if m.end() == m.start():
            # matches zero length string; this will never progress
            raise RuntimeEvaluationError("D1004", "Regular expression matches zero length string")
        yield m
        cursor = m.end()


def match_closure(compiled: _regex_mod.Pattern, s: str, m: _regex_mod.Match) -> dict[str, Any]:
    """Builds the matcher-closure object jsonata's own `evaluateRegex`
    closure yields -- {match, start, end, groups, next} in that key order,
    with `next` a zero-arity function value continuing the search.

    Ported from jsonata2js src/runtime/regex.js:76-100 (`matchFrom`). This
    is the object `$replace`'s FUNCTION replacer is handed verbatim
    (regex.js:186-190, verified against real jsonata), which is why it is
    NOT the {match, index, groups} shape `$match` remaps its results to:
    `$replace(s, re, function($m) { $m.start })` sees a number, and
    `$m.next` is callable. Non-participating groups stay None (JSON null),
    which is what JavaScript's `undefined` serialises to.

    Built lazily per match rather than eagerly for the whole scan: only
    the function-replacer path needs it, and that path already pays a
    lambda invocation per match.
    """
    return {
        "match": m.group(0),
        "start": m.start(),
        "end": m.end(),
        "groups": list(m.groups()),
        "next": JLambda(lambda _arg=MISSING: _closure_next(compiled, s, m.end()), 0),
    }


def _closure_next(compiled: _regex_mod.Pattern, s: str, cursor: int) -> Any:
    """`matchFrom`'s installed `next()` (regex.js:88-96): stops once the
    cursor has reached the end of the subject, and raises D1004 when the
    match it would return is zero length -- an unadvanced cursor would
    repeat it forever."""
    if cursor >= len(s):
        return MISSING
    m = compiled.search(s, cursor)
    if m is None:
        return MISSING
    if m.end() == m.start():
        raise RuntimeEvaluationError("D1004", "Regular expression matches zero length string")
    return match_closure(compiled, s, m)


def build_literal_regex(s: str) -> _regex_mod.Pattern:
    """Builds a pattern that matches the literal string s (no special
    regex chars)."""
    return _regex_mod.compile(_regex_mod.escape(s))
