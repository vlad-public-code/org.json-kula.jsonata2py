"""Numeric built-in functions for JSONata, delegated from core.py.

Ported from org.json_kula.jsonata_jvm.runtime.numeric.NumericBuiltins.

Implements: $number (with radix-literal and NaN/Infinity validation),
$round (precision + banker's rounding), $random, $formatBase,
$formatNumber (decimal_picture.py), $formatInteger/$parseInteger
(integer_picture.py, english_words.py).
"""

from __future__ import annotations

import math
import random as _random
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from ...errors import _RuntimeEvaluationError as RuntimeEvaluationError
from .. import core as _core
from ..values import MISSING, is_function, is_regex

# =============================================================================
# $number -- with 0x / 0o / 0b radix-literal support; NaN guard
# =============================================================================


def fn_number(arg: Any) -> Any:
    if arg is MISSING:
        return MISSING
    if _core.is_number(arg):
        d = float(arg)
        if d != d or d in (float("inf"), float("-inf")):
            raise RuntimeEvaluationError("D3030", "$number: value out of range for number type")
        return arg
    if isinstance(arg, bool):
        return _core.num_node(1 if arg else 0)
    if arg is None or isinstance(arg, (list, dict)) or is_function(arg) or is_regex(arg):
        raise RuntimeEvaluationError("T0410", "$number: argument is not a valid value for $number")
    if isinstance(arg, str):
        s = arg.strip()
        try:
            neg = s.startswith("-")
            abs_s = s[1:] if neg else s
            if abs_s[:2] in ("0x", "0X"):
                d = (-1.0 if neg else 1.0) * int(abs_s[2:], 16)
            elif abs_s[:2] in ("0o", "0O"):
                d = (-1.0 if neg else 1.0) * int(abs_s[2:], 8)
            elif abs_s[:2] in ("0b", "0B"):
                d = (-1.0 if neg else 1.0) * int(abs_s[2:], 2)
            else:
                d = float(s)
            if d != d or d in (float("inf"), float("-inf")):
                raise RuntimeEvaluationError("D3030", "$number: value out of range for number type")
            return _core.num_node(d)
        except (ValueError, IndexError):
            raise RuntimeEvaluationError("D3030", f"$number: unable to cast value to a number: {s}") from None
    raise RuntimeEvaluationError("D3030", "$number: unable to cast value to a number")


# =============================================================================
# $round -- precision + half-to-even (banker's rounding)
# =============================================================================

_POWERS_OF_TEN = [10.0**i for i in range(16)]


def fn_round(number: Any, precision: Any = MISSING) -> Any:
    if number is MISSING:
        return MISSING
    v = _core.to_number(number)
    if v != v or v in (float("inf"), float("-inf")):
        return v
    p = 0 if precision is MISSING else int(_core.to_number(precision))
    fast = _round_without_decimal_arithmetic(v, p)
    if fast is not None:
        return _core.num_node(fast)
    bd = Decimal(repr(v)).quantize(Decimal(1).scaleb(-p), rounding=ROUND_HALF_EVEN)
    return _core.num_node(float(bd))


def _round_without_decimal_arithmetic(v: float, p: int) -> float | None:
    """Rounds half-to-even in binary floating point, or returns None when
    the exact decimal answer might differ (near a tie)."""
    if p < 0 or p > 15:
        return None
    factor = _POWERS_OF_TEN[p]
    scaled = v * factor
    if abs(scaled) >= 1e15:
        return None
    fraction = abs(scaled - _floor(scaled))
    if abs(fraction - 0.5) < 1e-9:
        return None
    return _rint(scaled) / factor


def _floor(x: float) -> float:
    import math

    return math.floor(x)


def _rint(x: float) -> float:
    """Round half to even, matching Java's Math.rint."""
    import math

    floor = math.floor(x)
    diff = x - floor
    if diff < 0.5:
        return floor
    if diff > 0.5:
        return floor + 1
    return floor if floor % 2 == 0 else floor + 1


# =============================================================================
# $random
# =============================================================================


def fn_random() -> Any:
    return _random.random()


# =============================================================================
# $formatBase
# =============================================================================

_DIGITS = "0123456789abcdefghijklmnopqrstuvwxyz"


def fn_formatBase(number: Any, radix: Any = MISSING) -> Any:
    if number is MISSING:
        return MISSING
    n = round(_core.to_number(number))
    r = 10 if radix is MISSING else int(_core.to_number(radix))
    if r < 2 or r > 36:
        raise RuntimeEvaluationError("D3100", "$formatBase: radix must be between 2 and 36")
    return _to_base(n, r)


def _to_base(n: int, r: int) -> str:
    if n == 0:
        return "0"
    neg = n < 0
    n = abs(n)
    digits = []
    while n:
        digits.append(_DIGITS[n % r])
        n //= r
    if neg:
        digits.append("-")
    return "".join(reversed(digits))


# =============================================================================
# $formatNumber / $formatInteger / $parseInteger -- Phase 5c (not yet ported)
# =============================================================================


def _opt_char(opts: Any, key: str, default: str) -> str:
    if opts is MISSING or not isinstance(opts, dict):
        return default
    v = opts.get(key)
    if not isinstance(v, str) or v == "":
        return default
    return v[0]


def _opt_str(opts: Any, key: str, default: str) -> str:
    if opts is MISSING or not isinstance(opts, dict):
        return default
    v = opts.get(key)
    return v if isinstance(v, str) else default


def fn_formatNumber(number: Any, picture: Any, options: Any = MISSING) -> Any:
    from . import decimal_picture

    if number is MISSING or picture is MISSING:
        return MISSING

    v = _core.to_number(number)
    pic = _core.to_text(picture)

    decimal_sep = _opt_char(options, "decimal-separator", ".")
    group_sep = _opt_char(options, "grouping-separator", ",")
    exponent_sep = _opt_char(options, "exponent-separator", "e")
    percent = _opt_str(options, "percent", "%")
    per_mille = _opt_str(options, "per-mille", "‰")
    zero_digit = _opt_char(options, "zero-digit", "0")
    digit_char = _opt_char(options, "digit", "#")
    pattern_sep = _opt_char(options, "pattern-separator", ";")
    minus_sign = _opt_str(options, "minus-sign", "-")

    sep_idx = pic.find(pattern_sep)
    pos_pic = pic[:sep_idx] if sep_idx >= 0 else pic
    neg_pic = pic[sep_idx + 1 :] if sep_idx >= 0 else None
    if neg_pic is not None and pattern_sep in neg_pic:
        raise RuntimeEvaluationError(
            "D3080", "$formatNumber: the picture string must not contain more than one instance of the pattern separator"
        )

    has_percent = percent in pos_pic
    has_per_mille = per_mille in pos_pic
    is_neg = v < 0
    if has_percent:
        work = abs(v) * 100
    elif has_per_mille:
        work = abs(v) * 1000
    else:
        work = abs(v)

    active_pic = neg_pic if (is_neg and neg_pic is not None) else pos_pic
    result = decimal_picture.format(
        work, active_pic, decimal_sep, group_sep, exponent_sep, percent, per_mille, zero_digit, digit_char
    )

    if is_neg and neg_pic is None:
        result = minus_sign + result
    return result


def fn_formatInteger(number: Any, picture: Any) -> Any:
    from . import integer_picture

    if number is MISSING or picture is MISSING:
        return MISSING
    num_double = _core.to_number(number)
    if math.isinf(num_double) or math.isnan(num_double):
        raise RuntimeEvaluationError(None, f"$formatInteger: value is not representable as an integer: {num_double}")
    pic = _core.to_text(picture)
    # Numbers beyond long range are only representable via word pictures.
    if num_double > 9223372036854775807 or num_double < -9223372036854775808:
        return integer_picture.format_large(num_double, pic)
    return integer_picture.format(int(num_double), pic)


def fn_parseInteger(string: Any, picture: Any) -> Any:
    from . import integer_picture

    if string is MISSING or picture is MISSING:
        return MISSING
    s = _core.to_text(string)
    pic = _core.to_text(picture)
    return _core.num_node(integer_picture.parse(s, pic))
