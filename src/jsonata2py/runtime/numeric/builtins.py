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
import re
from typing import Any

from ...errors import _RuntimeEvaluationError as RuntimeEvaluationError
from .. import core as _core
from ..values import MISSING, is_function, is_regex

# =============================================================================
# $number -- with 0x / 0o / 0b radix-literal support; NaN guard
# =============================================================================

# $number's accepted string forms, ported from jsonata2js `numeric.js:30,36`.
# Deliberately narrower than Python's float()/int(): no leading "+", no
# surrounding whitespace, digits required on BOTH sides of ".", no digit
# underscores, and a radix literal may not carry a sign.
_DECIMAL_LITERAL = re.compile(r"^-?[0-9]+(\.[0-9]+)?([Ee][-+]?[0-9]+)?$")
_RADIX_LITERAL = re.compile(r"^(0[xX][0-9A-Fa-f]+)|(0[oO][0-7]+)|(0[bB][0-1]+)$")


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
        # Python's own float()/int() accept far more than JSONata does:
        # leading "+", surrounding whitespace, a bare ".5" or "5.", digit
        # underscores ("1_000") and a *signed* radix literal. The two
        # grammars below are ported verbatim from jsonata2js
        # `numeric.js:30,36` (which in turn match upstream jsonata's
        # functions.js), so $number("+1") / " 1 " / ".5" / "5." now raise
        # D3030 the way the reference does.
        if _DECIMAL_LITERAL.match(arg):
            d = float(arg)
        elif _RADIX_LITERAL.match(arg):
            base = {"x": 16, "o": 8, "b": 2}[arg[1].lower()]
            d = float(int(arg[2:], base))
        else:
            raise RuntimeEvaluationError("D3030", f"$number: unable to cast value to a number: {arg}")
        # One chained comparison, matching fn_round: catches NaN (every
        # comparison False) and both infinities without the per-call
        # BUILD_TUPLE that `d in (_INF, _NEG_INF)` would emit, since the
        # operands are globals rather than foldable literals.
        if not (_NEG_INF < d < _INF):
            raise RuntimeEvaluationError("D3030", "$number: value out of range for number type")
        return _core.num_node(d)
    raise RuntimeEvaluationError("D3030", "$number: unable to cast value to a number")


# =============================================================================
# $round -- precision + half-to-even (banker's rounding)
# =============================================================================

_POWERS_OF_TEN = [10.0**i for i in range(16)]
_INF = float("inf")
_NEG_INF = float("-inf")
_floor = math.floor


def fn_round(number: Any, precision: Any = MISSING) -> Any:
    """$round(number [, precision]) -- half-to-even (banker's rounding).

    Two guards decide whether the cheap binary-float path may answer.

    The tie tolerance **scales with the magnitude**, which the fixed
    1e-9 it replaced did not. `v * 10**p` is accurate to half an ulp of
    the product, and at the old 1e15 ceiling an ulp is 0.125 -- eight
    orders of magnitude wider than the window meant to catch a tie -- so
    the fast path would sail past a genuine .5 and answer confidently
    with the wrong value ($round(-36435.03133177965, 10) scaled to
    ...796.56 where the true product is ...796.5, and returned
    -36435.0313317797 instead of -36435.0313317796). jsonata2js avoids
    this by pairing a 1e9 ceiling with a 1e-6 window (`numeric.js:106`);
    deriving the window from the ulp instead keeps the wider ceiling and
    is correct at every magnitude.

    Anything the fast path declines goes to the reference's own
    algorithm rather than to exact decimal arithmetic. The two disagree
    in the region where the requested precision exceeds what the double
    can carry, and there output parity is the contract -- exact-decimal
    rounding differed from the reference on 298 of 6,000 randomised
    high-precision cases.
    """
    if number is MISSING:
        return MISSING
    v = _core.to_number(number)
    # One chained comparison catches NaN (every comparison False) and both
    # infinities. `v in (_INF, _NEG_INF)` would read more directly but
    # BUILD_TUPLE runs per call, since the operands are globals rather
    # than literals the peephole pass can fold. It also stays correct for
    # an int too large to convert to a float, where math.isfinite raises.
    if not (_NEG_INF < v < _INF):
        return v
    p = 0 if precision is MISSING else int(_core.to_number(precision))
    if 0 <= p <= 15:
        factor = _POWERS_OF_TEN[p]
        scaled = v * factor
        if -1e15 < scaled < 1e15:
            # 8 ulp of margin over the half-ulp the multiplication can
            # introduce; 2**-52 is the relative step of a double.
            tol = (scaled if scaled >= 0.0 else -scaled) * 1.8e-15
            if tol < 1e-9:
                tol = 1e-9
            floor = _floor(scaled)
            offset = (scaled - floor) - 0.5
            if offset <= -tol:
                return _core.num_node(floor / factor)
            if offset >= tol:
                return _core.num_node((floor + 1) / factor)
            # Too close to a tie to call in binary.
    from . import decimal_picture

    # decimal_picture owns the port of jsonata's own $round (numeric.js:106-133);
    # sharing it is what keeps $round and $formatNumber's rounding identical.
    return _core.num_node(decimal_picture.round_half_even(v, p))


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


def fn_formatNumber(number: Any, picture: Any, options: Any = MISSING) -> Any:
    # Every part of the algorithm -- the decimal-format option overrides, the
    # `;` sub-picture split, sign selection and percent/per-mille scaling --
    # lives in decimal_picture.format_number, because F&O 4.7.5 orders those
    # steps in a way that is observable: the sign-appropriate sub-picture is
    # chosen first (bullet 2) and only *that* sub-picture is scanned for a
    # percent or per-mille sign (bullet 3), so `"0.0;(0.0)%"` scales negative
    # values by 100 and positive values not at all.
    from . import decimal_picture

    if number is MISSING or picture is MISSING:
        return MISSING

    return decimal_picture.format_number(
        _core.to_number(number),
        _core.to_text(picture),
        options if isinstance(options, dict) else None,
    )


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
    # math.floor, not int(): int() truncates toward zero, so a negative
    # non-integral input rounded the wrong way. The reference does
    # Math.floor (jsonata2js `datetime.js:260`), making
    # $formatInteger(-12.6, '###0') "-13" rather than "-12". Positives
    # are unaffected.
    return integer_picture.format(math.floor(num_double), pic)


def fn_parseInteger(string: Any, picture: Any) -> Any:
    from . import integer_picture

    if string is MISSING or picture is MISSING:
        return MISSING
    s = _core.to_text(string)
    pic = _core.to_text(picture)
    return _core.num_node(integer_picture.parse(s, pic))
