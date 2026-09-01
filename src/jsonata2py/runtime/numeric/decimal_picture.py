"""Core picture-string formatter for $formatNumber.

Ported from jsonata2js `src/runtime/numeric.js:162-554` (`fn_formatNumber`),
itself a verbatim port of upstream jsonata's implementation of the XPath 3.1
F&O `fn:format-number` algorithm: F&O 4.7.3 validation (`validate`), 4.7.4
analysis (`analyse`) and 4.7.5 formatting (bullets 1-14).

This replaces an earlier port of the JVM implementation, which formatted
through a `java.text.DecimalFormat`-shaped pipeline. That pipeline is not
what the reference interpreter does, and it diverged from it observably for
a zero value under an all-optional-digit picture, for a decimal separator
with no digits after it, for the `percent` / `per-mille` / `zero-digit`
option overrides, for sub-picture selection when the value is negative, and
for `Number.prototype.toFixed`'s behaviour at and above 1e21.

The algorithm below therefore follows the JS line for line, including the
three places where upstream is quirky -- the reference's output depends on
all three, so they are ported deliberately:

  * `percent` / `per-mille` are NOT active characters (numeric.js:193-199).
    Overriding `percent` to something that does not occur in the picture
    makes a literal `%` in that picture an ordinary suffix character and
    suppresses the x100 scaling entirely.
  * when a sub-picture has no decimal separator, the *suffix* is used as
    the fractional part (numeric.js:241).
  * `getGroupingPositions` always advances its scan cursor through
    `integerPart`, even while analysing the fractional part
    (numeric.js:357).
"""

from __future__ import annotations

import math
from functools import lru_cache
from typing import Any, NamedTuple

from ...errors import _RuntimeEvaluationError as RuntimeEvaluationError

# numeric.js:165-177 -- note these are full strings, not single characters:
# `percent`, `per-mille`, `minus-sign`, `infinity` and `NaN` are compared and
# concatenated whole. (`infinity` and `NaN` are accepted and ignored, exactly
# as upstream does.)
_DEFAULTS: dict[str, str] = {
    "decimal-separator": ".",
    "grouping-separator": ",",
    "exponent-separator": "e",
    "infinity": "Infinity",
    "minus-sign": "-",
    "NaN": "NaN",
    "percent": "%",
    "per-mille": "\u2030",
    "zero-digit": "0",
    "digit": "#",
    "pattern-separator": ";",
}

_MESSAGES: dict[str, str] = {
    "D3080": "$formatNumber: the picture string must not contain more than one instance of the pattern separator",
    "D3081": "$formatNumber: there must only be one decimal separator in the picture string",
    "D3082": "$formatNumber: there must only be one percent character in the picture string",
    "D3083": "$formatNumber: there must only be one per-mille character in the picture string",
    "D3084": "$formatNumber: a picture string must not contain both a percent and a per-mille character",
    "D3085": "$formatNumber: the picture string must contain at least one digit or zero-digit placeholder",
    "D3086": "$formatNumber: an invalid character appeared in the sub-picture",
    "D3087": "$formatNumber: a grouping separator must not be adjacent to a decimal separator",
    "D3088": "$formatNumber: a grouping separator must not appear at the end of the integer part of the picture string",
    "D3089": "$formatNumber: a grouping separator must not be adjacent to another grouping separator",
    "D3090": "$formatNumber: an optional digit character must not appear after a mandatory digit character in "
    "the integer part of the picture string",
    "D3091": "$formatNumber: a mandatory digit character must not appear after an optional digit character in "
    "the fractional part of the picture string",
    "D3092": "$formatNumber: a percent or per-mille character must not appear in the exponent part of the picture string",
    "D3093": "$formatNumber: the exponent part of the picture string must contain at least one digit and nothing else",
}


def _err(code: str) -> RuntimeEvaluationError:
    return RuntimeEvaluationError(code, _MESSAGES[code])


class _Parts(NamedTuple):
    """Result of `splitParts` (numeric.js:207-256)."""

    subpicture: str
    prefix: str
    suffix: str
    active_part: str
    mantissa_part: str
    exponent_part: str | None
    integer_part: str
    frac_part: str


class _SubPicture(NamedTuple):
    """Result of `analyse` (numeric.js:412-424).

    The grouping positions are tuples where the reference uses arrays: a
    `_SubPicture` is cached by `_analyse_picture`, so it has to be immutable
    all the way down or one `$formatNumber` call could corrupt the next.
    """

    int_group_positions: tuple[int, ...]
    regular_grouping: int
    min_int_size: int
    scaling_factor: int
    prefix: str
    frac_group_positions: tuple[int, ...]
    min_frac_size: int
    max_frac_size: int
    min_exp_size: int
    suffix: str
    picture: str
    # Precomputed from `picture` so bullets 3, 5 and 12 do not re-scan it and
    # do not re-evaluate `10 ** scalingFactor` on every call.
    scale: int  # bullet 3: 1, 100 (percent) or 1000 (per-mille)
    max_mantissa: float  # bullet 5: 10 ** scaling_factor
    min_mantissa: float  # bullet 5: 10 ** (scaling_factor - 1)
    has_decimal_sep: bool  # bullet 12


class _Analysed(NamedTuple):
    """Everything `format_number` needs that depends only on the picture
    string and the decimal-format overrides: F&O 4.7.2 - 4.7.4 in full."""

    minus_sign: str
    zero_digit: str
    decimal_sep: str
    grouping_sep: str
    exponent_sep: str
    percent: str
    per_mille: str
    family: tuple[str, ...]
    positive: _SubPicture
    negative: _SubPicture


# A real picture string, and a real decimal-format override, is at most a few
# dozen characters. Anything longer is not a format spec -- and a picture, and
# every option value, can be a field of the input document, so both lengths
# are attacker-controlled. `lru_cache(maxsize=N)` bounds the entry COUNT, not
# the key size, so N oversized keys would be retained in full: 256 keys of
# 200 KB is ~51 MB for this one cache. Over-long keys therefore take the
# uncached path: same answer, slower, O(1) memory.
#
# The limit is on the whole cache key -- the picture plus every override key
# and value -- so the cache can retain at most 256 x 256 characters.
_MAX_CACHEABLE_PICTURE = 256


def _analyse_picture(picture: str, overrides: tuple[tuple[str, str], ...]) -> _Analysed:
    """F&O 4.7.2 - 4.7.4, memoized unless the key is too big to retain."""
    key_size = len(picture)
    for key, val in overrides:
        key_size += len(key) + len(val)
    if key_size <= _MAX_CACHEABLE_PICTURE:
        return _analyse_picture_cached(picture, overrides)
    return _analyse_picture_uncached(picture, overrides)


@lru_cache(maxsize=256)
def _analyse_picture_cached(picture: str, overrides: tuple[tuple[str, str], ...]) -> _Analysed:
    """F&O 4.7.2 - 4.7.4: split, validate and analyse both sub-pictures.

    Memoized on the picture string plus the decimal-format overrides, which
    arrive normalised to a tuple of pairs because a dict is unhashable. The
    reference re-runs the whole analysis on every call (numeric.js:428-441),
    which is pure waste: the picture is a literal at essentially every call
    site. Every field of the result is immutable, so a cached entry cannot be
    mutated by the caller.

    D3080 and the 4.7.3 validation errors escape the cache, since `lru_cache`
    does not memoise exceptions -- which is what we want: they stay raised on
    every call.
    """
    if overrides:
        properties = _DEFAULTS.copy()
        properties.update(overrides)
        if not properties["zero-digit"]:
            # An empty zero-digit makes bullet 7's leading/trailing strip
            # loops spin forever in the reference (`''.charAt(0)` is `''`,
            # and `''.substring(1)` is `''`). Fall back rather than hang.
            properties["zero-digit"] = _DEFAULTS["zero-digit"]
    else:
        properties = _DEFAULTS

    zero_digit = properties["zero-digit"]
    decimal_sep = properties["decimal-separator"]
    grouping_sep = properties["grouping-separator"]
    exponent_sep = properties["exponent-separator"]
    percent = properties["percent"]
    per_mille = properties["per-mille"]
    digit_char = properties["digit"]
    pattern_sep = properties["pattern-separator"]

    # numeric.js:187-191
    zero_cp = ord(zero_digit[0])
    family = tuple([chr(cp) for cp in range(zero_cp, zero_cp + 10)])
    family_set = frozenset(family)
    # A digit-or-optional-digit test used all over `_analyse`.
    digits_or_optional = family_set | {digit_char}

    # numeric.js:193-199 -- percent and per-mille are deliberately absent.
    active_chars = family_set | {decimal_sep, exponent_sep, grouping_sep, digit_char, pattern_sep}

    # `String.prototype.split('')` splits into single characters, where
    # `str.split('')` raises; spelled out so an empty `pattern-separator`
    # reaches the same D3080 the reference reports.
    sub_pictures = list(picture) if pattern_sep == "" else picture.split(pattern_sep)
    if len(sub_pictures) > 2:
        raise _err("D3080")

    parts = [_split_parts(sp, active_chars, exponent_sep, decimal_sep) for sp in sub_pictures]
    for p in parts:
        _validate(p, active_chars, family_set, decimal_sep, grouping_sep, digit_char, percent, per_mille)

    variables = [
        _analyse(p, family_set, digits_or_optional, grouping_sep, digit_char, percent, per_mille, decimal_sep)
        for p in parts
    ]

    # numeric.js:437-440 -- a single sub-picture implies a negative one that
    # is identical except for a leading minus sign.
    minus_sign = properties["minus-sign"]
    if len(variables) == 1:
        variables.append(variables[0]._replace(prefix=minus_sign + variables[0].prefix))

    return _Analysed(
        minus_sign,
        zero_digit,
        decimal_sep,
        grouping_sep,
        exponent_sep,
        percent,
        per_mille,
        family,
        variables[0],
        variables[1],
    )


# `lru_cache` wraps with `functools.update_wrapper`, so `__wrapped__` is the
# undecorated function: the identical analysis with no entry retained.
_analyse_picture_uncached = _analyse_picture_cached.__wrapped__


def format_number(value: float, picture: str, options: Any = None) -> str:
    """`$formatNumber(value, picture [, options])`.

    `value` is the signed number, `picture` the whole picture string
    (both sub-pictures, if there are two), and `options` either a decimal
    format override mapping or None.
    """
    if options:
        # Upstream assigns every entry of `options` unconditionally, so a
        # non-string value there makes the reference throw a *JavaScript*
        # TypeError rather than a JSONata error; there is no behaviour to
        # match, so non-strings fall back to the default instead.
        #
        # A dict is unhashable, so the overrides are normalised to a tuple of
        # pairs for the cache key. Insertion order is canonical: a dict cannot
        # hold a duplicate key, so two dicts with the same entries in a
        # different order only cost a cache miss, never a wrong answer.
        #
        # A non-string key is dropped too. Every name the algorithm reads is a
        # string literal, so such an entry could never be read anyway, and
        # dropping it keeps the cache-key size measurable.
        overrides = tuple([(k, v) for k, v in options.items() if isinstance(k, str) and isinstance(v, str)])
    else:
        overrides = ()

    analysed = _analyse_picture(picture, overrides)
    minus_sign = analysed.minus_sign
    zero_digit = analysed.zero_digit
    decimal_sep = analysed.decimal_sep
    grouping_sep = analysed.grouping_sep
    exponent_sep = analysed.exponent_sep
    family = analysed.family

    # bullet 2 -- the sign-appropriate sub-picture is chosen FIRST, and only
    # the chosen one was scanned for percent / per-mille (numeric.js:443-458),
    # which is why the scaling factor is a per-sub-picture field.
    pic = analysed.positive if value >= 0 else analysed.negative

    # bullet 3
    scale = pic.scale
    adjusted = value if scale == 1 else value * scale

    # bullet 5
    exponent: int | None = None
    if pic.min_exp_size == 0:
        mantissa = adjusted
    else:
        max_mantissa = pic.max_mantissa
        min_mantissa = pic.min_mantissa
        mantissa = adjusted
        exponent = 0
        # F&O bullet 5: for a zero value M and E are both zero. Skipping the
        # loops also guards against spinning forever on it.
        if mantissa != 0:
            while abs(mantissa) < min_mantissa:
                mantissa *= 10
                exponent -= 1
            while abs(mantissa) > max_mantissa:
                mantissa /= 10
                exponent += 1

    # bullet 6
    max_frac = pic.max_frac_size
    rounded = _round_half_even(mantissa, max_frac)

    # bullet 7
    string_value = _make_string(rounded, max_frac, zero_digit, family)
    decimal_pos = string_value.find(".")
    if decimal_pos == -1:
        string_value += decimal_sep
    else:
        string_value = string_value.replace(".", decimal_sep, 1)
    while string_value[:1] == zero_digit:
        string_value = string_value[1:]
    while string_value[-1:] == zero_digit:
        string_value = string_value[:-1]

    # bullets 8 & 9
    decimal_pos = string_value.find(decimal_sep)
    pad_left = pic.min_int_size - decimal_pos
    pad_right = pic.min_frac_size - (len(string_value) - decimal_pos - 1)
    if pad_left > 0:
        string_value = zero_digit * pad_left + string_value
    if pad_right > 0:
        string_value += zero_digit * pad_right
    decimal_pos = string_value.find(decimal_sep)

    # bullet 10
    regular_grouping = pic.regular_grouping
    if regular_grouping > 0:
        group_count = (decimal_pos - 1) // regular_grouping
        for group in range(1, group_count + 1):
            at = decimal_pos - group * regular_grouping
            string_value = string_value[:at] + grouping_sep + string_value[at:]
    else:
        for pos in pic.int_group_positions:
            at = decimal_pos - pos
            string_value = string_value[:at] + grouping_sep + string_value[at:]
            decimal_pos += 1

    # bullet 11
    decimal_pos = string_value.find(decimal_sep)
    for pos in pic.frac_group_positions:
        at = pos + decimal_pos + 1
        string_value = string_value[:at] + grouping_sep + string_value[at:]

    # bullet 12
    if not pic.has_decimal_sep or string_value.find(decimal_sep) == len(string_value) - 1:
        string_value = string_value[: len(string_value) - 1]

    # bullet 13
    if exponent is not None:
        string_exponent = _make_string(exponent, 0, zero_digit, family)
        exp_pad_left = pic.min_exp_size - len(string_exponent)
        if exp_pad_left > 0:
            string_exponent = zero_digit * exp_pad_left + string_exponent
        string_value += exponent_sep + (minus_sign if exponent < 0 else "") + string_exponent

    # bullet 14
    return pic.prefix + string_value + pic.suffix


# =============================================================================
# F&O 4.7.2 -- splitting a sub-picture (numeric.js:207-256)
# =============================================================================


def _split_parts(subpicture: str, active_chars: frozenset[str], exponent_sep: str, decimal_sep: str) -> _Parts:
    prefix = ""
    for ii, ch in enumerate(subpicture):
        if ch in active_chars and ch != exponent_sep:
            prefix = subpicture[:ii]
            break

    suffix = ""
    for ii in range(len(subpicture) - 1, -1, -1):
        ch = subpicture[ii]
        if ch in active_chars and ch != exponent_sep:
            suffix = subpicture[ii + 1 :]
            break

    active_part = subpicture[len(prefix) : len(subpicture) - len(suffix)]

    exponent_position = subpicture.find(exponent_sep, len(prefix))
    if exponent_position == -1 or exponent_position > len(subpicture) - len(suffix):
        mantissa_part = active_part
        exponent_part: str | None = None
    else:
        # numeric.js:235-236 slices `activePart` with a `subpicture`-relative
        # index; ported as-is because the reference's output depends on it.
        mantissa_part = active_part[:exponent_position]
        exponent_part = active_part[exponent_position + 1 :]

    decimal_position = mantissa_part.find(decimal_sep)
    if decimal_position == -1:
        integer_part = mantissa_part
        frac_part = suffix  # numeric.js:241
    else:
        integer_part = mantissa_part[:decimal_position]
        frac_part = mantissa_part[decimal_position + 1 :]

    return _Parts(subpicture, prefix, suffix, active_part, mantissa_part, exponent_part, integer_part, frac_part)


# =============================================================================
# F&O 4.7.3 -- validation (numeric.js:259-345)
# =============================================================================


def _char_at(s: str, i: int) -> str:
    """`String.prototype.charAt`: the empty string when i is out of range
    (including the negative index JS never wraps)."""
    return s[i] if 0 <= i < len(s) else ""


def _validate(
    parts: _Parts,
    active_chars: frozenset[str],
    family_set: frozenset[str],
    decimal_sep: str,
    grouping_sep: str,
    digit_char: str,
    percent: str,
    per_mille: str,
) -> None:
    # Upstream assigns to a single `error` variable and throws once at the
    # end, so when several rules are broken the LAST one wins. Ported as-is.
    error: str | None = None
    subpicture = parts.subpicture

    decimal_pos = subpicture.find(decimal_sep)
    if decimal_pos != subpicture.rfind(decimal_sep):
        error = "D3081"
    if subpicture.find(percent) != subpicture.rfind(percent):
        error = "D3082"
    if subpicture.find(per_mille) != subpicture.rfind(per_mille):
        error = "D3083"
    if percent in subpicture and per_mille in subpicture:
        error = "D3084"

    valid = False
    for ch in parts.mantissa_part:
        if ch in family_set or ch == digit_char:
            valid = True
            break
    if not valid:
        error = "D3085"

    for ch in parts.active_part:
        if ch not in active_chars:
            error = "D3086"
            break

    if decimal_pos != -1:
        if (
            _char_at(subpicture, decimal_pos - 1) == grouping_sep
            or _char_at(subpicture, decimal_pos + 1) == grouping_sep
        ):
            error = "D3087"
    elif _char_at(parts.integer_part, len(parts.integer_part) - 1) == grouping_sep:
        error = "D3088"

    if grouping_sep + grouping_sep in subpicture:
        error = "D3089"

    optional_digit_pos = parts.integer_part.find(digit_char)
    if optional_digit_pos != -1 and _count_in(parts.integer_part[:optional_digit_pos], family_set) > 0:
        error = "D3090"

    optional_digit_pos = parts.frac_part.rfind(digit_char)
    if optional_digit_pos != -1 and _count_in(parts.frac_part[optional_digit_pos:], family_set) > 0:
        error = "D3091"

    exponent_part = parts.exponent_part
    if exponent_part is not None:
        if exponent_part and (percent in subpicture or per_mille in subpicture):
            error = "D3092"
        if not exponent_part or _count_not_in(exponent_part, family_set) > 0:
            error = "D3093"

    if error is not None:
        raise _err(error)


# =============================================================================
# F&O 4.7.4 -- analysis (numeric.js:348-425)
# =============================================================================


def _count_in(s: str, chars: frozenset[str]) -> int:
    n = 0
    for c in s:
        if c in chars:
            n += 1
    return n


def _count_not_in(s: str, chars: frozenset[str]) -> int:
    n = 0
    for c in s:
        if c not in chars:
            n += 1
    return n


def _grouping_positions(
    part: str, integer_part: str, to_left: bool, grouping_sep: str, counted: frozenset[str]
) -> tuple[int, ...]:
    positions: list[int] = []
    grouping_position = part.find(grouping_sep)
    while grouping_position != -1:
        segment = part[:grouping_position] if to_left else part[grouping_position:]
        positions.append(_count_in(segment, counted))
        # numeric.js:357 advances through `integerPart` even when `part` is
        # the fractional part. Ported as-is: the reference depends on it.
        grouping_position = integer_part.find(grouping_sep, grouping_position + 1)
    return tuple(positions)


def _regular(indexes: tuple[int, ...]) -> int:
    """The common grouping interval, or 0 when the positions are irregular
    (numeric.js:362-379)."""
    if not indexes:
        return 0
    factor = indexes[0]
    for k in range(1, len(indexes)):
        factor = math.gcd(factor, indexes[k])
    for index in range(1, len(indexes) + 1):
        if index * factor not in indexes:
            return 0
    return factor


def _analyse(
    parts: _Parts,
    family_set: frozenset[str],
    digits_or_optional: frozenset[str],
    grouping_sep: str,
    digit_char: str,
    percent: str,
    per_mille: str,
    decimal_sep: str,
) -> _SubPicture:
    integer_part = parts.integer_part
    frac_part = parts.frac_part

    int_group_positions = _grouping_positions(integer_part, integer_part, False, grouping_sep, digits_or_optional)
    regular_grouping = _regular(int_group_positions)
    frac_group_positions = _grouping_positions(frac_part, integer_part, True, grouping_sep, digits_or_optional)

    min_int_size = _count_in(integer_part, family_set)
    scaling_factor = min_int_size

    min_frac_size = _count_in(frac_part, family_set)
    max_frac_size = _count_in(frac_part, digits_or_optional)

    exponent_part = parts.exponent_part
    exponent_present = exponent_part is not None
    if min_int_size == 0 and max_frac_size == 0:
        if exponent_present:
            min_frac_size = 1
            max_frac_size = 1
        else:
            min_int_size = 1
    if exponent_present and min_int_size == 0 and digit_char in integer_part:
        min_int_size = 1
    if min_int_size == 0 and min_frac_size == 0:
        min_frac_size = 1

    min_exp_size = _count_in(exponent_part, family_set) if exponent_part is not None else 0

    subpicture = parts.subpicture

    # bullet 3 (numeric.js:445-452) -- only the chosen sub-picture is scanned,
    # so the factor belongs to the sub-picture, not to the analysis as a whole.
    if percent in subpicture:
        scale = 100
    elif per_mille in subpicture:
        scale = 1000
    else:
        scale = 1

    # bullet 5. Left at 0.0 when there is no exponent part, because the
    # reference never evaluates the power in that case and `10.0 ** n` raises
    # OverflowError past n == 308 -- a picture from user data could reach it.
    if min_exp_size:
        max_mantissa = 10.0**scaling_factor
        min_mantissa = 10.0 ** (scaling_factor - 1)
    else:
        max_mantissa = 0.0
        min_mantissa = 0.0

    return _SubPicture(
        int_group_positions,
        regular_grouping,
        min_int_size,
        scaling_factor,
        parts.prefix,
        frac_group_positions,
        min_frac_size,
        max_frac_size,
        min_exp_size,
        parts.suffix,
        subpicture,
        scale,
        max_mantissa,
        min_mantissa,
        decimal_sep in subpicture,
    )


# =============================================================================
# ECMAScript numeric primitives the algorithm is defined in terms of
# =============================================================================


def _js_number_to_string(x: float) -> str:
    """`Number.prototype.toString()` for the magnitudes that reach it here
    (|x| >= 1e21, where both ECMAScript and CPython print the shortest
    round-tripping decimal in exponential form) plus the infinities."""
    if x == math.inf:
        return "Infinity"
    if x == -math.inf:
        return "-Infinity"
    return repr(x)


def _to_fixed(ax: float, dp: int) -> str:
    """`Number.prototype.toFixed(dp)` for a non-negative `ax`.

    ECMA-262 20.1.3.3: pick the integer n minimising |n / 10**dp - ax|,
    breaking ties towards the larger n -- i.e. exact half-up on the binary
    value, which is what the integer-ratio arithmetic below computes. At and
    above 1e21 toFixed falls back to `ToString`, which is why the reference
    emits things like `1e+21.00` for `$formatNumber(1e21, "#,##0.00")`.
    """
    if ax != ax:
        return "NaN"
    if ax >= 1e21:
        return _js_number_to_string(ax)
    num, den = ax.as_integer_ratio()
    q, r = divmod(num * 10**dp, den)
    if 2 * r >= den:
        q += 1
    s = str(q)
    if dp == 0:
        return s
    if len(s) <= dp:
        s = "0" * (dp - len(s) + 1) + s
    cut = len(s) - dp
    return s[:cut] + "." + s[cut:]


def _js_math_round(x: float) -> float:
    """`Math.round`: the closest integral value, ties towards +Infinity."""
    if not (-math.inf < x < math.inf):
        return x
    f = math.floor(x)
    # x - f is exact for every finite x (it is a multiple of ulp(x) below 1).
    return float(f) if x - f < 0.5 else float(f + 1)


def _shift_decimal_exponent(value: float, by: int) -> float:
    """Shifts value's decimal exponent through its own shortest decimal
    string, so no float-multiplication noise is introduced (numeric.js:70-75).
    CPython's `repr` and ECMAScript's `ToString` pick different
    exponential-notation thresholds but the same digits, so both spellings
    reparse to the same double here."""
    s = repr(value)
    e = s.find("e")
    if e == -1:
        return float(s + "e" + str(by))
    return float(s[:e] + "e" + str(int(s[e + 1 :]) + by))


def _round_half_even(arg: float, precision: int) -> float:
    """jsonata's `$round` -- round-half-to-even at `precision` decimal places
    (numeric.js:106-133, slow path). `precision == 0` is falsy in JS, so the
    exponent shift is skipped for it exactly as upstream skips it."""
    if not (-math.inf < arg < math.inf):
        return arg
    if precision:
        arg = _shift_decimal_exponent(arg, precision)
    result = _js_math_round(arg)
    diff = result - arg
    if (diff == 0.5 or diff == -0.5) and abs(math.fmod(result, 2.0)) == 1.0:
        # Rounded the wrong way -- adjust to the nearest even number.
        result -= 1
    if precision:
        result = _shift_decimal_exponent(result, -precision)
    return 0.0 if result == 0 else result


def round_half_even(arg: float, precision: int) -> float:
    """Public alias: `numeric/builtins.py` `fn_round` shares this, because it
    is the port of jsonata's own `$round` (numeric.js:106-133) and sharing it
    is what keeps `$round` and `$formatNumber` rounding identically."""
    return _round_half_even(arg, precision)


def _make_string(val: float, dp: int, zero_digit: str, family: tuple[str, ...]) -> str:
    """numeric.js:486-495 -- the magnitude at `dp` decimal places, in the
    configured digit family."""
    s = _to_fixed(abs(val), dp)
    if zero_digit != "0":
        return "".join([family[ord(c) - 48] if "0" <= c <= "9" else c for c in s])
    return s
