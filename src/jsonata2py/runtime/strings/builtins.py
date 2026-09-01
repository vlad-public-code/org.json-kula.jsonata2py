"""String built-in functions for JSONata, delegated from core.py.

Ported from org.json_kula.jsonata_jvm.runtime.string.StringBuiltins.

The code-point compensation layer Java needs (UTF-16 code units vs.
JSONata's code-point semantics) is deleted per D4/7.3: Python str is
already indexed by code point, so $length/$substring/etc. become plain
len(s) and s[a:b]. The *clamping* logic (clamp index, negative-start
handling, length <= 0 -> "") is kept -- that's JSONata semantics, not a
Java workaround.

Regex-heavy helpers (`$match`, `$replace`, `$split` with a regex
separator, `$contains` with a regex) use regex_ops.py, which reports
character offsets directly (no byte-offset conversion layer -- see D4).
"""

from __future__ import annotations

import base64
from typing import Any

import regex as _regex_mod

from ...errors import _RuntimeEvaluationError as RuntimeEvaluationError
from .. import core as _core
from ..values import MISSING, is_function, is_regex
from . import regex_ops as _regex_ops
from . import url_codec as _url_codec

# See fn_trim: the literal class jsonata normalises on.
_TRIM_RUN = _regex_mod.compile(r"[ \t\n\r]+")

# Stands in for jsonata's `limit === undefined` in the $match/$split/$replace
# match loops, which all read `limit === undefined || count < limit`. Folding
# the absent case into the comparison keeps one branch out of the per-match
# loop; `inf` is never reachable as a real limit (a jsonata number literal
# out of double range is rejected at parse time).
_INF = float("inf")


def _to_uint32(limit: Any) -> int:
    """ECMAScript ToUint32 -- truncate toward zero, then take the value
    modulo 2**32. Only `String.prototype.split`'s limit argument needs it
    (jsonata2js src/runtime/string.js:230); every other JSONata limit is
    compared as a real number. Callers have already rejected negatives."""
    f = float(limit)
    if f != f or f == _INF:  # NaN / Infinity -> 0, per ToUint32
        return 0
    return int(f) & 0xFFFFFFFF


# See fn_base64decode: everything Node's base64 decoder silently drops, and
# the URL-safe alias mapping it accepts alongside the standard alphabet.
_B64_ALIEN = _regex_mod.compile(r"[^A-Za-z0-9+/_-]")
_B64_URLSAFE = str.maketrans("-_", "+/")


# =============================================================================
# $uppercase / $lowercase
# =============================================================================


def fn_uppercase(arg: Any) -> Any:
    if arg is MISSING:
        return MISSING
    if not isinstance(arg, str):
        raise RuntimeEvaluationError("T0410", "$uppercase() function: argument 1 of $uppercase must be a string")
    return arg.upper()


def fn_lowercase(arg: Any) -> Any:
    if arg is MISSING:
        return MISSING
    if not isinstance(arg, str):
        raise RuntimeEvaluationError("T0410", "$lowercase() function: argument 1 of $lowercase must be a string")
    return arg.lower()


# =============================================================================
# $trim
# =============================================================================


def fn_trim(arg: Any) -> Any:
    if arg is MISSING:
        return MISSING
    if not isinstance(arg, str):
        raise RuntimeEvaluationError("T0410", "$trim() function: argument 1 of $trim must be a string")
    # JSONata collapses runs of exactly these FOUR characters -- not the
    # Unicode whitespace class. `\s` under `regex`/`re` also matches NBSP,
    # U+2028, U+3000, \v and \f, all of which JSONata leaves untouched.
    # Ported from jsonata2js src/runtime/string.js:163-169, including the
    # trailing step: at most ONE leading and ONE trailing space is
    # stripped (charAt/substring), which is not str.strip().
    result = _TRIM_RUN.sub(" ", arg)
    if result[:1] == " ":
        result = result[1:]
    if result[-1:] == " ":
        result = result[:-1]
    return result


# =============================================================================
# $length
# =============================================================================


def fn_length(arg: Any) -> Any:
    if arg is MISSING:
        return MISSING
    if not isinstance(arg, str):
        raise RuntimeEvaluationError("T0410", "$length: argument must be a string")
    return len(arg)


def fn_length_ctx(arg: Any) -> Any:
    if arg is MISSING:
        return MISSING
    if not isinstance(arg, str):
        raise RuntimeEvaluationError("T0411", "$length: context value must be a string")
    return len(arg)


# =============================================================================
# $substring
# =============================================================================


def fn_substring(str_: Any, start: Any, length: Any = MISSING) -> Any:
    if str_ is MISSING:
        return MISSING
    if not isinstance(str_, str):
        raise RuntimeEvaluationError("T0410", "$substring() function: argument 1 of $substring must be a string")
    if not _core.is_number(start):
        raise RuntimeEvaluationError("T0410", "$substring() function: argument 2 of $substring must be a number")
    cp_len = len(str_)
    cp_begin = _core.clamp_index(int(_core.to_number(start)), cp_len)
    if length is MISSING:
        return str_[cp_begin:]
    if not _core.is_number(length):
        raise RuntimeEvaluationError("T0410", "$substring() function: argument 3 of $substring must be a number")
    raw_len = int(_core.to_number(length))
    if raw_len <= 0:
        return ""
    cp_end = min(cp_begin + raw_len, cp_len)
    if cp_begin >= cp_end:
        return ""
    return str_[cp_begin:cp_end]


# =============================================================================
# $substringBefore / $substringAfter
# =============================================================================


def fn_substringBefore(str_: Any, chars: Any) -> Any:
    return _substring_before(str_, chars, ctx=False)


def fn_substringBefore_ctx(str_: Any, chars: Any) -> Any:
    return _substring_before(str_, chars, ctx=True)


def _substring_before(str_: Any, chars: Any, ctx: bool) -> Any:
    if str_ is MISSING or chars is MISSING:
        return MISSING
    if not isinstance(str_, str):
        code = "T0411" if ctx else "T0410"
        subject = "context value" if ctx else "argument 1"
        raise RuntimeEvaluationError(code, f"$substringBefore() function: {subject} of $substringBefore must be a string")
    if not isinstance(chars, str):
        raise RuntimeEvaluationError("T0410", "$substringBefore() function: argument 2 of $substringBefore must be a string")
    idx = str_.find(chars)
    return str_ if idx < 0 else str_[:idx]


def fn_substringAfter(str_: Any, chars: Any) -> Any:
    return _substring_after(str_, chars, ctx=False)


def fn_substringAfter_ctx(str_: Any, chars: Any) -> Any:
    return _substring_after(str_, chars, ctx=True)


def _substring_after(str_: Any, chars: Any, ctx: bool) -> Any:
    if str_ is MISSING or chars is MISSING:
        return MISSING
    if not isinstance(str_, str):
        code = "T0411" if ctx else "T0410"
        subject = "context value" if ctx else "argument 1"
        raise RuntimeEvaluationError(code, f"$substringAfter() function: {subject} of $substringAfter must be a string")
    if not isinstance(chars, str):
        raise RuntimeEvaluationError("T0410", "$substringAfter() function: argument 2 of $substringAfter must be a string")
    idx = str_.find(chars)
    return str_ if idx < 0 else str_[idx + len(chars) :]


# =============================================================================
# $contains
# =============================================================================


def fn_contains(str_: Any, search: Any) -> Any:
    if str_ is MISSING or search is MISSING:
        return MISSING
    if not isinstance(str_, str):
        raise RuntimeEvaluationError("T0410", "Argument 1 of $contains must be a string")
    if is_regex(search):
        return _regex_ops.compiled_of(search).search(str_) is not None
    if not isinstance(search, str):
        raise RuntimeEvaluationError("T0410", "Argument 2 of $contains must be a string or regex")
    return search in str_


# =============================================================================
# $split
# =============================================================================


def fn_split(str_: Any, separator: Any, limit: Any = MISSING) -> Any:
    if str_ is MISSING:
        return MISSING
    if separator is MISSING:
        separator = ""
    if not isinstance(str_, str):
        raise RuntimeEvaluationError("T0410", "$split: argument 1 must be a string")
    if is_function(separator):
        raise RuntimeEvaluationError("T1010", "The separator argument of $split must be a string or regular expression")
    if not is_regex(separator) and not isinstance(separator, str):
        raise RuntimeEvaluationError("T0410", "$split: argument 2 must be a string or regex")
    if limit is not MISSING:
        if not _core.is_number(limit):
            raise RuntimeEvaluationError("T0410", "$split: argument 3 must be a number")
        if float(limit) < 0:
            raise RuntimeEvaluationError("D3020", "$split: limit must be non-negative")

    if is_regex(separator):
        # Ported from jsonata2js src/runtime/regex.js:291-321. The cursor is
        # advanced by iter_matches (which owns the zero-length D1004 guard);
        # note that next() is pulled BEFORE re-testing `limit`, so a limit
        # does not suppress that D1004.
        #
        # `limit` is compared as a REAL number (`count < limit`), never
        # truncated: $split("a,b,c,d", /,/, 1.5) yields two separators'
        # worth of splits, not one. `inf` stands in for jsonata's
        # `limit === undefined`, so the two `limit === undefined ||`
        # disjunctions collapse to a single comparison.
        lim = _INF if limit is MISSING else float(limit)
        result: list[str] = []
        if lim > 0:
            matches = _regex_ops.iter_matches(_regex_ops.compiled_of(separator), str_)
            m = next(matches, None)
            if m is None:
                result.append(str_)
            else:
                start = 0
                count = 0
                while m is not None and count < lim:
                    result.append(str_[start : m.start()])
                    start = m.end()
                    m = next(matches, None)
                    count += 1
                if count < lim:
                    result.append(str_[start:])
        return result

    # The plain-string-separator overload is NOT the same loop: jsonata
    # delegates it to `String.prototype.split(separator, limit)` (see
    # jsonata2js src/runtime/string.js:230), whose limit argument goes
    # through ToUint32 -- so 1.5 truncates to 1 and 2**32 + 0.5 wraps to 0.
    ilim = -1 if limit is MISSING else _to_uint32(limit)
    sep = _core.to_text(separator)
    strs: list[str] = []
    if sep == "":
        n = len(str_) if ilim < 0 else min(ilim, len(str_))
        for k in range(n):
            strs.append(str_[k])
        return strs
    pos = 0
    count = 0
    while True:
        idx = str_.find(sep, pos)
        if idx < 0:
            break
        if ilim >= 0 and count >= ilim:
            break
        strs.append(str_[pos:idx])
        count += 1
        pos = idx + len(sep)
    if ilim < 0 or count < ilim:
        strs.append(str_[pos:])
    return strs


# =============================================================================
# $join
# =============================================================================


def fn_join(arr: Any, separator: Any = MISSING) -> Any:
    if arr is MISSING:
        return MISSING
    if separator is not MISSING and not isinstance(separator, str):
        raise RuntimeEvaluationError("T0410", "$join: separator argument must be a string")
    if not isinstance(arr, list):
        if not isinstance(arr, str):
            raise RuntimeEvaluationError("T0412", "$join: function argument must be an array of strings")
        return arr
    sep = separator if isinstance(separator, str) else ""
    parts = []
    for elem in arr:
        if not isinstance(elem, str):
            raise RuntimeEvaluationError("T0412", "$join: function argument must be an array of strings")
        parts.append(elem)
    return sep.join(parts)


# =============================================================================
# $match
# =============================================================================


def fn_match(str_: Any, pattern: Any, limit: Any = MISSING) -> Any:
    if str_ is MISSING or pattern is MISSING:
        return MISSING
    # regex.js:153-155 -- $match's own non-negative-limit guard, the D3040
    # counterpart of $split's D3020 and $replace's D3011. It is checked
    # AFTER the undefined-subject short-circuit, matching regex.js:150.
    if limit is not MISSING:
        if not _core.is_number(limit):
            raise RuntimeEvaluationError("T0410", "$match: argument 3 must be a number")
        if float(limit) < 0:
            raise RuntimeEvaluationError("D3040", "$match: limit must be non-negative")
    # Compared as a REAL number, never truncated: $match("aaa", /a/, 1.5)
    # yields TWO matches, because jsonata tests `count < limit` directly.
    lim = _INF if limit is MISSING else float(limit)
    s = _core.to_text(str_)

    if is_function(pattern):
        return _match_with_lambda(s, pattern, lim)

    compiled = _regex_ops.compiled_of(pattern) if is_regex(pattern) else _regex_ops.build_literal_regex(
        _core.to_text(pattern)
    )
    results: list[dict[str, Any]] = []
    if lim > 0:
        # Ported from jsonata2js src/runtime/regex.js:149-170. `groups` holds
        # the raw group values, so a group that did not participate stays
        # absent (JSON null) rather than becoming "" -- real jsonata leaves
        # JavaScript's `undefined` there, which serialises as null.
        matches = _regex_ops.iter_matches(compiled, s)
        m = next(matches, None)
        count = 0
        while m is not None and count < lim:
            results.append({"match": m.group(0), "index": m.start(), "groups": list(m.groups())})
            m = next(matches, None)
            count += 1
    # createSequence()/RT.collapse(result, false): no match -> undefined,
    # exactly one match -> the bare object, not a one-element array.
    return _core.unwrap(results)


def _match_with_lambda(s: str, pattern: Any, limit: float) -> Any:
    """$match with a custom matcher function (not a regex/string pattern).

    Protocol (ported from RegexOps.matchWithLambda): the pattern function
    is called ONCE with the whole subject string; every subsequent call
    uses the 'next' continuation the PREVIOUS result returned (called with
    JSONata null as its argument), not the original pattern re-applied to
    a substring. Each result must be an object with match/start/end
    (required) and groups/next (optional); iteration stops when a result
    is missing, isn't an object, is missing match/start/end, or has no
    function-valued 'next'.
    """
    from ..lambdas import fn_apply

    results: list[dict[str, Any]] = []
    count = 0
    current_pattern = pattern
    first_call = True

    while count < limit:
        result = fn_apply(current_pattern, s if first_call else None)
        first_call = False

        if result is MISSING or not isinstance(result, dict):
            break

        match = result.get("match", MISSING)
        start = result.get("start", MISSING)
        end = result.get("end", MISSING)
        groups = result.get("groups", MISSING)
        nxt = result.get("next", MISSING)

        if match is MISSING or start is MISSING or end is MISSING:
            break

        results.append(
            {
                "match": _core.to_text(match),
                "index": int(_core.to_number(start)),
                "groups": [] if groups is MISSING else groups,
            }
        )
        count += 1

        if nxt is not MISSING and is_function(nxt):
            current_pattern = nxt
        else:
            break

    # Same sequence collapse as the regex path: regex.js:169 applies
    # RT.collapse to whichever matcher produced the results.
    return _core.unwrap(results)


# =============================================================================
# $replace
# =============================================================================


def fn_replace(str_: Any, pattern: Any, replacement: Any, limit: Any = MISSING) -> Any:
    if str_ is MISSING or pattern is MISSING or replacement is MISSING:
        return MISSING
    if not isinstance(str_, str):
        raise RuntimeEvaluationError("T0410", "$replace: argument 1 must be a string")
    if not is_regex(pattern) and not isinstance(pattern, str):
        raise RuntimeEvaluationError("T0410", "$replace: argument 2 must be a string or regex")
    if not is_function(replacement) and not isinstance(replacement, str):
        raise RuntimeEvaluationError("T0410", "$replace: argument 3 must be a string or function")
    if limit is not MISSING:
        if not _core.is_number(limit):
            raise RuntimeEvaluationError("T0410", "$replace: argument 4 must be a number")
        if float(limit) < 0:
            raise RuntimeEvaluationError("D3011", "$replace: limit must be non-negative")
    s = str_
    if not is_regex(pattern) and pattern == "":
        raise RuntimeEvaluationError("D3010", "$replace: second argument cannot be an empty string")
    compiled = _regex_ops.compiled_of(pattern) if is_regex(pattern) else _regex_ops.build_literal_regex(pattern)
    # Compared as a REAL number, never truncated (regex.js:270): a limit of
    # 1.5 replaces TWO matches.
    lim = _INF if limit is MISSING else float(limit)

    from ..lambdas import fn_apply

    # Ported from jsonata2js src/runtime/regex.js:247-285. The zero-length
    # guard lives in iter_matches and fires only on a *subsequent* empty
    # match, so `$replace("abc", /$/, "-")` -> "abc-" is legal while
    # `$replace("abc", /^/, "-")` still raises D1004.
    out: list[str] = []
    pos = 0
    if lim > 0:
        matches = _regex_ops.iter_matches(compiled, s)
        m = next(matches, None)
        if m is None:
            out.append(s)
        else:
            count = 0
            is_fn = is_function(replacement)
            template = None if is_fn else _core.to_text(replacement)
            while m is not None and count < lim:
                start = m.start()
                out.append(s[pos:start])
                if template is None:
                    # regex.js:186-190: the function replacer is handed the
                    # RAW matcher-closure object -- {match, start, end,
                    # groups, next}, next() included -- NOT the remapped
                    # {match, index, groups} shape $match returns, and with
                    # non-participating groups left null rather than "".
                    rep_result = fn_apply(replacement, _regex_ops.match_closure(compiled, s, m))
                    if not isinstance(rep_result, str):
                        # Includes a replacer that returned nothing: jsonata
                        # tests `typeof replacedWith === 'string'`, which
                        # `undefined` fails just like a number would.
                        raise RuntimeEvaluationError("D3012", "$replace: replacement function must return a string")
                    out.append(rep_result)
                else:
                    out.append(_expand_replacement(template, m))
                pos = m.end()
                count += 1
                m = next(matches, None)
            out.append(s[pos:])
    else:
        out.append(s)
    return "".join(out)


def _expand_replacement(template: str, m: Any) -> str:
    """Expands $0, $1, ... $$ references in a $replace string replacement
    against a regex match object.

    Verified against the official test suite (regex/case015, case026,
    case028, case029) rather than guessed: for "$" followed by a run of
    digits, try the first TWO digits as a group number; if that's not a
    valid group (0 = whole match, or 1..total_groups), try just the FIRST
    digit; if that's ALSO invalid, that one digit is dropped (consumed,
    produces nothing) -- in every case any digits of the run past the
    ones actually consumed as the group number remain as literal text.
    The "$" itself is always consumed, never emitted literally except via
    the "$$" escape.
    """
    total_groups = m.re.groups
    out: list[str] = []
    i = 0
    n = len(template)

    def group_text(num: int) -> str:
        if num == 0:
            return str(m.group(0))
        g = m.group(num)
        return g if g is not None else ""

    while i < n:
        c = template[i]
        if c == "$" and i + 1 < n:
            nxt = template[i + 1]
            if nxt == "$":
                out.append("$")
                i += 2
                continue
            if nxt.isdigit():
                j = i + 1
                while j < n and template[j].isdigit():
                    j += 1
                digits = template[i + 1 : j]  # the whole consecutive digit run
                consumed = 0
                group_num = None
                if len(digits) >= 2:
                    two = int(digits[:2])
                    if two == 0 or 1 <= two <= total_groups:
                        group_num, consumed = two, 2
                if group_num is None:
                    one = int(digits[0])
                    if one == 0 or 1 <= one <= total_groups:
                        group_num, consumed = one, 1
                if group_num is not None:
                    out.append(group_text(group_num))
                else:
                    consumed = 1  # invalid single digit: drop it, produce nothing
                out.append(digits[consumed:])  # leftover digits are literal
                i += 1 + len(digits)
                continue
        out.append(c)
        i += 1
    return "".join(out)


# =============================================================================
# $pad
# =============================================================================


def fn_pad(str_: Any, width: Any, pad_char: Any = MISSING) -> Any:
    if str_ is MISSING or width is MISSING:
        return MISSING
    if not isinstance(str_, str):
        raise RuntimeEvaluationError("T0410", "$pad: argument 1 must be a string")
    w = int(_core.to_number(width))
    pc = " " if pad_char is MISSING else _core.to_text(pad_char)
    if pc == "":
        pc = " "
    cp_len = len(str_)
    pc_cp_len = len(pc)
    abs_w = abs(w)
    if cp_len >= abs_w:
        return str_
    need = abs_w - cp_len
    padding: list[str] = []
    added = 0
    while added < need:
        take = min(pc_cp_len, need - added)
        padding.append(pc[:take])
        added += take
    pad = "".join(padding)
    return str_ + pad if w >= 0 else pad + str_


# =============================================================================
# $eval
# =============================================================================


def fn_eval(expr: Any, context: Any = MISSING) -> Any:
    from .. import context as _ctx

    if expr is MISSING:
        return MISSING
    delegate = _ctx.get_eval_delegate()
    if delegate is None:
        raise RuntimeEvaluationError(None, "$eval: no eval delegate registered (create a JsonataExpressionFactory first)")
    ctx = MISSING if context is MISSING else context
    return delegate(_core.to_text(expr), ctx)


# =============================================================================
# $string (prettify variant)
# =============================================================================


def fn_string_prettify(arg: Any) -> Any:
    if arg is MISSING:
        return MISSING
    if isinstance(arg, str):
        return arg
    sanitized = _core.sanitize_for_string(arg)
    return _core.json_encode_pretty(sanitized)


# =============================================================================
# $base64encode / $base64decode
# =============================================================================


def fn_base64encode(str_: Any) -> Any:
    if str_ is MISSING:
        return MISSING
    if not isinstance(str_, str):
        raise RuntimeEvaluationError("T0410", "$base64encode: argument must be a string")
    # btoa / Buffer.from(str, 'binary') semantics (jsonata2js
    # src/runtime/codec.js:15): every UTF-16 code UNIT contributes its low
    # byte, `unit & 0xFF`. For code points below U+0100 that is exactly a
    # latin-1 encode -- one C call, and the case that actually occurs. For
    # anything else the string has to be expanded to UTF-16 first so that
    # an astral code point splits into its surrogate pair before the low
    # bytes are taken: "\U0001F600" -> units D83D,DE00 -> bytes 3D,00 ->
    # "PQA=". [::2] picks the low byte of each little-endian unit;
    # surrogatepass keeps a lone surrogate from raising.
    try:
        data = str_.encode("latin-1")
    except UnicodeEncodeError:
        data = str_.encode("utf-16-le", "surrogatepass")[::2]
    return base64.b64encode(data).decode("ascii")


def fn_base64decode(str_: Any) -> Any:
    if str_ is MISSING:
        return MISSING
    if not isinstance(str_, str):
        return MISSING
    # `Buffer.from(str, 'base64')` (codec.js:20) is LENIENT, and jsonata
    # inherits that: it never rejects input. Node's decoder
    #   * treats the first `=` as end-of-data ("QQ==QQ==" -> "A"),
    #   * silently drops every character outside the base64 alphabet,
    #     including whitespace, newlines and astral code points
    #     ("  QQ==  " -> "A", "!!!" -> ""),
    #   * accepts the URL-safe alphabet interchangeably ("Q-Q_" is
    #     "Q+Q/"), and
    #   * decodes a trailing partial group instead of demanding padding:
    #     4k+2 alphabet chars yield one extra byte ("QQ" -> "A"), 4k+3
    #     yield two ("QUJ" -> "AB"), and a lone 4k+1 char is discarded
    #     ("a" -> "").
    # validate=True rejected all of those, so $base64decode("QQ") raised
    # where the reference returns "A".
    data = str_.partition("=")[0]
    data = _B64_ALIEN.sub("", data)
    if "-" in data or "_" in data:
        data = data.translate(_B64_URLSAFE)
    rem = len(data) & 3
    if rem == 1:
        data = data[:-1]
    elif rem:
        data += "=" * (4 - rem)
    # Inverse of fn_base64encode: one UTF-16 code unit per byte with a zero
    # high byte, i.e. Buffer#toString('binary') / atob (codec.js:20). The
    # previous utf-8 decode was not the inverse of the encoder at all, so
    # even $base64decode($base64encode(x)) lost non-ASCII input.
    # `data` is now provably alphabet-only and correctly padded, so
    # b64decode cannot raise.
    return base64.b64decode(data).decode("latin-1")


# =============================================================================
# $encodeUrlComponent / $decodeUrlComponent / $encodeUrl / $decodeUrl
# =============================================================================


def fn_encodeUrlComponent(str_: Any) -> Any:
    if str_ is MISSING:
        return MISSING
    return _url_codec.encode(_core.to_text(str_), False)


def fn_decodeUrlComponent(str_: Any) -> Any:
    if str_ is MISSING:
        return MISSING
    try:
        return _url_codec.decode(_core.to_text(str_))
    except RuntimeEvaluationError as e:
        raise RuntimeEvaluationError(e.error_code, f"$decodeUrlComponent: {e.message}") from e


def fn_encodeUrl(str_: Any) -> Any:
    if str_ is MISSING:
        return MISSING
    return _url_codec.encode(_core.to_text(str_), True)


def fn_decodeUrl(str_: Any) -> Any:
    if str_ is MISSING:
        return MISSING
    try:
        return _url_codec.decode(_core.to_text(str_))
    except RuntimeEvaluationError as e:
        raise RuntimeEvaluationError(e.error_code, f"$decodeUrl: {e.message}") from e
