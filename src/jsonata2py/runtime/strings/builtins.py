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

from ...errors import _RuntimeEvaluationError as RuntimeEvaluationError
from .. import core as _core
from ..values import MISSING, is_function, is_regex
from . import regex_ops as _regex_ops
from . import url_codec as _url_codec

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
    import regex as _re

    return _re.sub(r"\s+", " ", arg).strip()


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
    lim = -1 if limit is MISSING else int(limit)

    if is_regex(separator):
        pattern = _regex_ops.compiled_of(separator)
        result: list[str] = []
        pos = 0
        count = 0
        n = len(str_)
        while pos <= n:
            if lim >= 0 and count >= lim:
                break
            m = pattern.search(str_, pos)
            if m is None:
                result.append(str_[pos:])
                break
            result.append(str_[pos : m.start()])
            count += 1
            end = m.end()
            pos = end if end > m.start() else end + 1
        return result

    sep = _core.to_text(separator)
    result = []
    if sep == "":
        n = len(str_) if lim < 0 else min(lim, len(str_))
        for k in range(n):
            result.append(str_[k])
        return result
    pos = 0
    count = 0
    while True:
        idx = str_.find(sep, pos)
        if idx < 0:
            break
        if lim >= 0 and count >= lim:
            break
        result.append(str_[pos:idx])
        count += 1
        pos = idx + len(sep)
    if lim < 0 or count < lim:
        result.append(str_[pos:])
    return result


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
    s = _core.to_text(str_)

    if is_function(pattern):
        lim = 2**31 - 1 if limit is MISSING else int(_core.to_number(limit))
        return _match_with_lambda(s, pattern, lim)

    compiled = _regex_ops.compiled_of(pattern) if is_regex(pattern) else _regex_ops.build_literal_regex(
        _core.to_text(pattern)
    )
    lim = 2**31 - 1 if limit is MISSING else int(_core.to_number(limit))
    results = []
    pos = 0
    count = 0
    n = len(s)
    while pos <= n and count < lim:
        m = compiled.search(s, pos)
        if m is None:
            break
        obj: dict[str, Any] = {
            "match": m.group(0),
            "index": m.start(),
            "groups": [g if g is not None else "" for g in m.groups()],
        }
        results.append(obj)
        count += 1
        end = m.end()
        pos = end if end > m.start() else end + 1
    return results if results else MISSING


def _match_with_lambda(s: str, pattern: Any, limit: int) -> Any:
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

    return results if results else MISSING


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
    lim = 2**31 - 1 if limit is MISSING else int(limit)

    from ..lambdas import fn_apply

    out: list[str] = []
    pos = 0
    count = 0
    n = len(s)
    while pos <= n and count < lim:
        m = compiled.search(s, pos)
        if m is None:
            break
        start, end = m.start(), m.end()
        if end == start:
            raise RuntimeEvaluationError("D1004", "Regular expression matches zero length string")
        out.append(s[pos:start])
        match_str = m.group(0)
        if is_function(replacement):
            match_obj = {
                "match": match_str,
                "index": start,
                "groups": [g if g is not None else "" for g in m.groups()],
            }
            rep_result = fn_apply(replacement, match_obj)
            if rep_result is not MISSING and not isinstance(rep_result, str):
                raise RuntimeEvaluationError("D3012", "$replace: replacement function must return a string")
            out.append(rep_result if isinstance(rep_result, str) else "")
        else:
            out.append(_expand_replacement(_core.to_text(replacement), m))
        count += 1
        pos = end
    if pos <= n:
        out.append(s[pos:])
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
    data = str_.encode("latin-1", errors="replace")
    return base64.b64encode(data).decode("ascii")


def fn_base64decode(str_: Any) -> Any:
    if str_ is MISSING:
        return MISSING
    if not isinstance(str_, str):
        return MISSING
    try:
        decoded = base64.b64decode(str_, validate=True)
    except Exception as e:
        raise RuntimeEvaluationError(None, f"$base64decode: invalid base64 input: {e}") from None
    return decoded.decode("utf-8", errors="replace")


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
