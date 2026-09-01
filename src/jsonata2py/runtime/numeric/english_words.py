"""Converts between numbers and English number words for the `w`, `W` and
`Ww` pictures of $formatInteger / $parseInteger.

Ported from jsonata2js `src/runtime/datetime.js:46-149` (`numberToWords`
and `wordsToNumber`), which is the reference jsonata implementation.

Two structural properties of the reference are load-bearing here:

  * Words are generated in **Title Case**. That is exactly what the `Ww`
    picture wants, so `Ww` needs no post-processing at all; `w` lowers
    and `W` uppers the result (datetime.js:301-307). Deriving title case
    from a lower-case string (the previous approach) is lossy -- "and"
    must stay lower-case while "One" must not.
  * The **sign is not handled here**. `$formatInteger` words the absolute
    value and prepends '-' itself (datetime.js:289-290, 349-351), so
    "minus" never appears in the reference's output.

The word separator is likewise the reference's, not "and" everywhere:
groups of hundreds and magnitudes are joined with ", " while a trailing
sub-hundred remainder is joined with " and ", so 1970 is "One Thousand,
Nine Hundred and Seventy".
"""

from __future__ import annotations

import math
import re

_FEW = (
    "Zero", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten",
    "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen",
    "Nineteen",
)
_ORDINALS = (
    "Zeroth", "First", "Second", "Third", "Fourth", "Fifth", "Sixth", "Seventh", "Eighth",
    "Ninth", "Tenth", "Eleventh", "Twelfth", "Thirteenth", "Fourteenth", "Fifteenth",
    "Sixteenth", "Seventeenth", "Eighteenth", "Nineteenth",
)
_DECADES = ("Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety", "Hundred")
_MAGNITUDES = ("Thousand", "Million", "Billion", "Trillion")
_MAG_COUNT = len(_MAGNITUDES)

# 10 ** (mag * 3) for mag 0..4, indexed by the magnitude clamp below.
_MAG_FACTORS = (1, 1_000, 1_000_000, 1_000_000_000, 1_000_000_000_000)
_MAG_FACTORS_DOUBLE = (1.0, 1e3, 1e6, 1e9, 1e12)

# =============================================================================
# Cardinal / ordinal word generation
# =============================================================================


def to_words(n: int, ordinal: bool) -> str:
    """English cardinal or ordinal words for a non-negative n, in Title
    Case. The caller supplies the sign."""
    return _lookup(n, False, ordinal)


def to_words_double(n: float, ordinal: bool) -> str:
    """English words for a non-negative float beyond long range."""
    return _lookup_double(n, False, ordinal)


def _lookup_double(num: float, prev: bool, ordinal: bool) -> str:
    """The same `lookup` (datetime.js:64-102) for a value beyond long
    range, kept in double arithmetic all the way down because the
    reference's numbers are doubles all the way down.

    Exact integer arithmetic would be wrong here, not merely different:
    the double spelled 1e46 is really 10000000000000000905969664 x 10^21,
    so dividing it exactly would produce a sentence about that value,
    where dividing it as a double gives 1e34 and ultimately "Ten Billion
    Trillion Trillion Trillion".

    Below a thousand the value is an exact small integer, so the shared
    integer `_lookup` takes over (and must, since it indexes tuples with
    the quotients).
    """
    if num < 1000.0:
        return _lookup(int(num), prev, ordinal)
    mag = int(math.log10(num) / 3)
    if mag > _MAG_COUNT:
        mag = _MAG_COUNT
    factor = _MAG_FACTORS_DOUBLE[mag]
    # Math.floor of the *rounded* quotient, which is not the same as
    # Python's float // (an exact floor); the reference divides first.
    mant = math.floor(num / factor)
    remainder = num - mant * factor
    words = (
        (", " if prev else "")
        + _lookup_double(float(mant), False, False)
        + " "
        + _MAGNITUDES[mag - 1]
    )
    if remainder > 0:
        return words + _lookup_double(remainder, True, ordinal)
    return words + "th" if ordinal else words


def _lookup(num: int, prev: bool, ordinal: bool) -> str:
    """Port of the `lookup` closure at datetime.js:64-102.

    `prev` says a more significant group has already been emitted, which
    selects the joining separator: " and " before a sub-hundred group,
    ", " before a hundreds or magnitude group.
    """
    if num <= 19:
        return (" and " if prev else "") + (_ORDINALS[num] if ordinal else _FEW[num])
    if num < 100:
        tens, remainder = divmod(num, 10)
        words = (" and " if prev else "") + _DECADES[tens - 2]
        if remainder > 0:
            return words + "-" + _lookup(remainder, False, ordinal)
        if ordinal:
            # "Twenty" -> "Twent" + "ieth"
            return words[:-1] + "ieth"
        return words
    if num < 1000:
        hundreds, remainder = divmod(num, 100)
        words = (", " if prev else "") + _FEW[hundreds] + " Hundred"
        if remainder > 0:
            return words + _lookup(remainder, True, ordinal)
        return words + "th" if ordinal else words
    # floor(log10(num) / 3) without the libm round-trip: for a positive
    # integer, floor(log10(num)) is len(str(num)) - 1, and adding the
    # discarded fraction back can never cross a multiple of 3.
    mag = (len(str(num)) - 1) // 3
    if mag > _MAG_COUNT:
        mag = _MAG_COUNT
    factor = _MAG_FACTORS[mag]
    mant, remainder = divmod(num, factor)
    words = (", " if prev else "") + _lookup(mant, False, False) + " " + _MAGNITUDES[mag - 1]
    if remainder > 0:
        return words + _lookup(remainder, True, ordinal)
    return words + "th" if ordinal else words


# =============================================================================
# Parsing English words -> number
# =============================================================================

_NAN = math.nan

# datetime.js:133 -- ", " / " and " / any single whitespace, backslash or hyphen.
_PARSE_SPLIT = re.compile(r",\s|\sand\s|[\s\\-]")

# datetime.js:107-125. Ordinal spellings are folded in alongside the
# cardinals, which is why $parseInteger needs no separate ordinal-suffix
# stripping for word pictures: "twelfth" and "twentieth" are keys.
_WORD_VALUES: dict[str, float] = {}
for _i, _w in enumerate(_FEW):
    _WORD_VALUES[_w.lower()] = _i
for _i, _w in enumerate(_ORDINALS):
    _WORD_VALUES[_w.lower()] = _i
for _i, _w in enumerate(_DECADES):
    _lw = _w.lower()
    _WORD_VALUES[_lw] = (_i + 2) * 10
    _WORD_VALUES[_lw[:-1] + "ieth"] = (_i + 2) * 10
_WORD_VALUES["hundredth"] = 100
for _i, _w in enumerate(_MAGNITUDES):
    _lw = _w.lower()
    # Float, as the reference's Math.pow is: it keeps "ten billion
    # trillion trillion trillion" at the double 1e46 rather than an exact
    # (and therefore unequal) Python bignum.
    _v = float(10 ** ((_i + 1) * 3))
    _WORD_VALUES[_lw] = _v
    _WORD_VALUES[_lw + "th"] = _v
del _i, _w, _lw, _v


def parse_words(s: str) -> int | float:
    """Port of `wordsToNumber` (datetime.js:132-149): a stack of segment
    totals, where a word below 100 accumulates into the top segment and a
    magnitude word multiplies it.

    An unrecognised word is `undefined` in the reference and poisons the
    arithmetic to NaN; NaN here reproduces that (it compares false against
    both 100 and 1000 exactly as `undefined` does).
    """
    segs: list[float] = [0]
    get = _WORD_VALUES.get
    for part in _PARSE_SPLIT.split(s):
        value = get(part, _NAN)
        if value < 100:
            top = segs.pop()
            if top >= 1000:
                segs.append(top)
                top = 0
            segs.append(top + value)
        else:
            segs.append(segs.pop() * value)
    total: float = 0
    for seg in segs:
        total += seg
    return total
