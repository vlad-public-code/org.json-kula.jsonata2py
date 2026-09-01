"""Picture-based integer formatting and parsing for $formatInteger and
$parseInteger (XPath 3.1 fn:format-integer, spec section 9.8.4).

Ported from jsonata2js `src/runtime/datetime.js`: `analyseIntegerPicture`
(:364-510), `_formatInteger` (:287-354) and the integer half of
`generateRegex` (:1067-1124), which is the reference jsonata
implementation. `runtime/datetime/picture_formatter.py` routes every
integer-valued date/time component through `format()` here, exactly as
the reference shares `_formatInteger` between `$formatInteger` and
`$fromMillis` (datetime.js:906).

Three structural properties of that algorithm are load-bearing, and the
previous java.text.DecimalFormat-derived implementation had none of them:

  * **The sign never participates.** `_formatInteger` formats
    `abs(value)` and prepends '-' at the very end, so the mandatory-digit
    width, the grouping separators, the ordinal-suffix lookup and the
    roman/alpha conversions all see the absolute value. Letting '-' count
    toward the width gave `$formatInteger(-7, "001")` -> "-07" instead of
    "-007", and letting it shift the ordinal lookup gave "-7rd" for
    `"1;o"`.
  * **Any Unicode decimal-digit family is a mandatory-digit sign.** The
    picture is scanned against a 37-entry table of family start code
    points (:357), not against ASCII plus a couple of hand-picked
    families; a character that is neither a digit nor '#' is a grouping
    separator, whatever it is.
  * **A picture with no mandatory digit is a "numbering sequence"**,
    which the reference does not implement -- so `"#"` alone raises
    D3130. It is not a decimal pattern of width zero.

The analysis is per-picture and pure, so it is memoised: a date/time
picture routes every integer-valued component through `format()` on every
`$fromMillis` call with the same picture string.
"""

from __future__ import annotations

import math
import re
from functools import lru_cache

from ...errors import _RuntimeEvaluationError as RuntimeEvaluationError
from . import english_words as _words

_NAN = math.nan

# Doubles hold integers exactly only up to 2**53; the reference's
# `parseInt` result is a double, so anything wider is a float here too.
_MAX_EXACT_DOUBLE = 9007199254740992

# =============================================================================
# Analysed picture
# =============================================================================

# format.primary
_DECIMAL = 0
_LETTERS = 1
_ROMAN = 2
_WORDS = 3
_SEQUENCE = 4

# format.case
_LOWER = 0
_UPPER = 1
_TITLE = 2

# datetime.js:357 -- the start code point of every Unicode decimal-digit
# family in the BMP. Flattened to a char -> family-start dict so the
# per-character lookup is one C-level dict probe instead of the
# reference's 37-iteration scan.
_DECIMAL_GROUPS = (
    0x30, 0x0660, 0x06F0, 0x07C0, 0x0966, 0x09E6, 0x0A66, 0x0AE6, 0x0B66, 0x0BE6,
    0x0C66, 0x0CE6, 0x0D66, 0x0DE6, 0x0E50, 0x0ED0, 0x0F20, 0x1040, 0x1090, 0x17E0,
    0x1810, 0x1946, 0x19D0, 0x1A80, 0x1A90, 0x1B50, 0x1BB0, 0x1C40, 0x1C50, 0xA620,
    0xA8D0, 0xA900, 0xA9D0, 0xA9F0, 0xAA50, 0xABF0, 0xFF10,
)
_DIGIT_GROUP: dict[str, int] = {
    chr(_start + _d): _start for _start in _DECIMAL_GROUPS for _d in range(10)
}


class _Picture:
    """The reference's analysed-picture object (datetime.js:365-370)."""

    __slots__ = (
        "case",
        "group_char",
        "group_position",
        "mandatory_digits",
        "ordinal",
        "primary",
        "regular",
        "separators",
        "token",
        "zero_code",
    )

    def __init__(self) -> None:
        self.primary = _DECIMAL
        self.case = _LOWER
        self.ordinal = False
        self.token = ""
        self.zero_code = 0x30
        self.mandatory_digits = 0
        self.regular = False
        self.group_position = 0
        self.group_char = ""
        # (position, character) pairs, ascending by position -- i.e. the
        # order the reference builds them in while scanning the picture
        # right to left. The formatter walks them in reverse.
        self.separators: tuple[tuple[int, str], ...] = ()


# A picture string can come from input data ($formatInteger(n, doc.pic)),
# so its length is caller-controlled and lru_cache's maxsize -- an ENTRY
# count -- does not bound the memory the cache retains. Measured: 60
# distinct 200 KB pictures retained 12.3 MB, and a full 256-entry cache
# of them would hold ~51 MB. A real format picture is tens of characters,
# so anything longer simply is not cached: same result, slower, O(1)
# memory.
_MAX_CACHEABLE_PICTURE = 256


def analyse(picture: str) -> _Picture:
    """Port of `analyseIntegerPicture` (datetime.js:364-510)."""
    if len(picture) > _MAX_CACHEABLE_PICTURE:
        return _analyse(picture)
    return _analyse_cached(picture)


@lru_cache(maxsize=256)
def _analyse_cached(picture: str) -> _Picture:
    return _analyse(picture)


def _analyse(picture: str) -> _Picture:
    """D3131 (mixed digit families) escapes the cache, since lru_cache
    does not memoise exceptions -- which is what we want: it stays raised
    on every call.
    """
    fmt = _Picture()

    semicolon = picture.rfind(";")
    if semicolon < 0:
        primary = picture
    else:
        primary = picture[:semicolon]
        if picture[semicolon + 1 : semicolon + 2] == "o":
            fmt.ordinal = True

    if primary == "w":
        fmt.primary = _WORDS
    elif primary == "W":
        fmt.case = _UPPER
        fmt.primary = _WORDS
    elif primary == "Ww":
        fmt.case = _TITLE
        fmt.primary = _WORDS
    elif primary == "i":
        fmt.primary = _ROMAN
    elif primary == "I":
        fmt.case = _UPPER
        fmt.primary = _ROMAN
    elif primary == "a":
        fmt.primary = _LETTERS
    elif primary == "A":
        fmt.case = _UPPER
        fmt.primary = _LETTERS
    else:
        _analyse_decimal(fmt, primary)
    return fmt


def _analyse_decimal(fmt: _Picture, primary: str) -> None:
    """The `default:` arm of the reference's switch (datetime.js:408-506).

    The picture is scanned right to left so that a separator's position is
    its digit offset from the least significant end.
    """
    zero_code = -1
    mandatory = 0
    separators: list[tuple[int, str]] = []
    sep_pos = 0
    group_of = _DIGIT_GROUP.get

    for char in reversed(primary):
        group = group_of(char)
        if group is not None:
            mandatory += 1
            sep_pos += 1
            if zero_code < 0:
                zero_code = group
            elif group != zero_code:
                raise RuntimeEvaluationError(
                    "D3131", "$formatInteger: picture string contains mixed digit families"
                )
        elif char == "#":  # optional-digit-sign
            sep_pos += 1
        else:
            # Neither a decimal-digit-sign nor an optional-digit-sign, so
            # by definition a grouping-separator-sign.
            separators.append((sep_pos, char))

    if mandatory == 0:
        # A numbering sequence; the spec leaves these implementation-
        # defined and the reference implements none.
        fmt.primary = _SEQUENCE
        fmt.token = primary
        return

    fmt.primary = _DECIMAL
    fmt.zero_code = zero_code
    fmt.mandatory_digits = mandatory

    regular = _regular_repeat(separators)
    if regular > 0:
        fmt.regular = True
        fmt.group_position = regular
        fmt.group_char = separators[0][1]
    else:
        fmt.separators = tuple(separators)


def _regular_repeat(separators: list[tuple[int, str]]) -> int:
    """Port of `regularRepeat` (datetime.js:460-487): the separators are
    regular when they all use the same character and sit at every multiple
    of the greatest common divisor of their positions."""
    if not separators:
        return 0
    sep_char = separators[0][1]
    for _pos, char in separators:
        if char != sep_char:
            return 0
    indexes = [pos for pos, _char in separators]
    factor = indexes[0]
    for index in indexes[1:]:
        factor = math.gcd(factor, index)
    for i in range(1, len(indexes) + 1):
        if i * factor not in indexes:
            return 0
    return factor


# =============================================================================
# Formatting
# =============================================================================

_SUFFIX_123 = {"1": "st", "2": "nd", "3": "rd"}


def format(n: int, pic: str) -> str:
    """Formats n using the given JSONata integer picture string."""
    return format_analysed(n, analyse(pic))


def format_analysed(value: int, fmt: _Picture) -> str:
    """Port of `_formatInteger` (datetime.js:287-354). The absolute value
    is formatted and the sign prepended afterwards, so nothing downstream
    ever sees a '-'."""
    negative = value < 0
    if negative:
        value = -value

    primary = fmt.primary
    if primary == _DECIMAL:
        formatted = _format_digits(str(value), fmt)
    elif primary == _WORDS:
        formatted = _apply_case(_words.to_words(value, fmt.ordinal), fmt.case)
    elif primary == _ROMAN:
        formatted = to_roman(value)
        if fmt.case == _UPPER:
            formatted = formatted.upper()
    elif primary == _LETTERS:
        formatted = to_alpha(value, fmt.case == _UPPER)
    else:
        raise RuntimeEvaluationError(
            "D3130", f'$formatInteger: unsupported numbering sequence "{fmt.token}"'
        )

    return "-" + formatted if negative else formatted


def _apply_case(words: str, case: int) -> str:
    """datetime.js:301-307. The words arrive in title case, which is
    exactly what `Ww` wants, so tcase.TITLE is left untouched; `w` lowers
    and `W` uppers. "and" is already lower case in the generated string,
    so title case keeps it that way for free."""
    if case == _LOWER:
        return words.lower()
    if case == _UPPER:
        return words.upper()
    return words


def _format_digits(digits: str, fmt: _Picture) -> str:
    """The `formats.DECIMAL` arm (datetime.js:309-345): pad to the
    mandatory width, shift into the picture's digit family, insert the
    grouping separators, then append the ordinal suffix."""
    mandatory = fmt.mandatory_digits
    pad = mandatory - len(digits)
    if pad > 0:
        digits = "0" * pad + digits

    zero_code = fmt.zero_code
    if zero_code != 0x30:
        delta = zero_code - 0x30
        digits = "".join([chr(ord(c) + delta) for c in digits])

    if fmt.regular:
        position = fmt.group_position
        char = fmt.group_char
        # `n` is computed once, from the unseparated length, but each
        # insertion point is measured against the grown string.
        for i in range((len(digits) - 1) // position, 0, -1):
            pos = len(digits) - i * position
            digits = digits[:pos] + char + digits[pos:]
    else:
        for position, char in reversed(fmt.separators):
            length = len(digits)
            pos = length - position
            if pos >= 0:
                digits = digits[:pos] + char + digits[pos:]
            else:
                # The reference uses String.prototype.substr, which
                # clamps: substr(0, pos) with pos < 0 is "", and
                # substr(pos) with pos < 0 counts back from the end
                # (datetime.js:330-332). A separator positioned beyond the
                # value's width therefore *discards* digits, e.g.
                # $formatInteger(-12345, "#:###,##0") -> "-,5".
                digits = char + digits[max(length + pos, 0) :]

    if fmt.ordinal:
        suffix = _SUFFIX_123.get(digits[-1:])
        if suffix is None or (len(digits) > 1 and digits[-2] == "1"):
            suffix = "th"
        digits += suffix

    return digits


def format_large(n: float, pic: str) -> str:
    """Formats a float beyond long range, where the reference is still
    operating on the same double it always was.

    Every picture but the roman one is reproducible here. `decimalToRoman`
    recurses once per emitted numeral, so at these magnitudes the
    reference does not produce a value at all -- it throws a RangeError
    from a blown stack -- and building the equivalent string here would
    burn unbounded memory for it. That one stays rejected.
    """
    fmt = analyse(pic)
    negative = n < 0
    if negative:
        n = -n

    primary = fmt.primary
    if primary == _WORDS:
        formatted = _apply_case(_words.to_words_double(n, fmt.ordinal), fmt.case)
    elif primary == _DECIMAL:
        from .. import core as _core

        # The reference stringifies with `'' + value`, so an exponential
        # rendering flows straight into the digit pipeline: 1e30 with
        # "#,##0" really does produce "1e,+30".
        formatted = _format_digits(_core.number_to_string(n), fmt)
    elif primary == _LETTERS:
        formatted = _to_alpha_double(n, fmt.case == _UPPER)
    elif primary == _SEQUENCE:
        raise RuntimeEvaluationError(
            "D3130", f'$formatInteger: unsupported numbering sequence "{fmt.token}"'
        )
    else:
        raise RuntimeEvaluationError(
            None, f"$formatInteger: value is not representable as an integer: {n}"
        )

    return "-" + formatted if negative else formatted


# =============================================================================
# Roman numerals
# =============================================================================

_ROMAN_NUMERALS = (
    (1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"),
    (50, "l"), (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i"),
)
_ROMAN_VALUES = {"M": 1000, "D": 500, "C": 100, "L": 50, "X": 10, "V": 5, "I": 1}

# The reference has no explicit limit: `decimalToRoman` recurses once per
# emitted numeral, so it survives a few thousand levels and then throws a
# RangeError. 10 million ('m' x 10000) is inside the region it handles;
# beyond that it would only ever have blown the stack, and building the
# string here instead would burn unbounded memory.
_ROMAN_MAX = 10_000_000


def to_roman(value: int) -> str:
    """Port of `decimalToRoman` (datetime.js:178-186), iterative. Returns
    lower case; the picture's case modifier upper-cases `I`. Zero (and,
    were it reachable, any negative) yields "", as the reference's
    exhausted recursion does."""
    if value <= 0:
        return ""
    if value > _ROMAN_MAX:
        raise RuntimeEvaluationError(
            None, f"$formatInteger: value too large for Roman numerals: {value}"
        )
    out: list[str] = []
    for val, symbol in _ROMAN_NUMERALS:
        while value >= val:
            out.append(symbol)
            value -= val
    return "".join(out)


def parse_roman(roman: str) -> int | float:
    """Port of `romanToDecimal` (datetime.js:193-207). An unrecognised
    character is `undefined` in the reference and poisons the running
    total to NaN, which is what the early return reproduces."""
    decimal = 0
    largest = 1
    get = _ROMAN_VALUES.get
    for digit in reversed(roman):
        value = get(digit)
        if value is None:
            return _NAN
        if value < largest:
            decimal -= value
        else:
            largest = value
            decimal += value
    return decimal


# =============================================================================
# Spreadsheet-style letters (A, B ... Z, AA, AB ...)
# =============================================================================


def to_alpha(value: int, upper: bool) -> str:
    """Port of `decimalToLetters` (datetime.js:219-227). Zero yields ""."""
    base = 65 if upper else 97
    out: list[str] = []
    while value > 0:
        value, rem = divmod(value - 1, 26)
        out.append(chr(base + rem))
    out.reverse()
    return "".join(out)


def _to_alpha_double(value: float, upper: bool) -> str:
    """`decimalToLetters` for a value beyond long range. Same loop, but
    the quotient must be re-narrowed to a double on every turn: the
    reference's `Math.floor((value - 1) / 26)` rounds the division before
    flooring it, and once the running value became an exact Python int the
    remaining digits would be those of a different number."""
    base = 65 if upper else 97
    out: list[str] = []
    while value > 0:
        out.append(chr(base + int((value - 1) % 26)))
        value = float(math.floor((value - 1) / 26))
    out.reverse()
    return "".join(out)


def parse_alpha(letters: str, upper: bool) -> int:
    """Port of `lettersToDecimal` (datetime.js:235-242).

    Deliberately unvalidated and case-sensitive, like the reference: it is
    plain char-code arithmetic against the picture's 'A'/'a', so a
    character outside the family contributes a negative digit
    ($parseInteger('-g', 'a') is -1319, not an error).
    """
    a_code = 65 if upper else 97
    decimal = 0
    for char in letters:
        decimal = decimal * 26 + (ord(char) - a_code + 1)
    return decimal


# =============================================================================
# Parsing
# =============================================================================

# ECMA-262 parseInt: leading whitespace, an optional sign, then either a
# hex literal or decimal digits; trailing junk is ignored.
_JS_INT = re.compile(r"([+-]?)(?:0[xX]([0-9a-fA-F]+)|([0-9]+))")


def parse(s: str, pic: str) -> int | float:
    """Parses a string back to a number using the given JSONata integer
    picture string. Port of `parseInteger` (datetime.js:1135-1144) plus
    the integer arm of `generateRegex` (:1067-1124).

    The reference never validates the input against the regex it builds
    (the `// TODO` at :1142), so malformed input reaches the same
    arithmetic and yields NaN rather than an error. Word pictures need no
    ordinal-suffix stripping: `wordValues` carries the ordinal spellings
    ("twelfth", "twentieth", "thousandth") alongside the cardinals.
    """
    fmt = analyse(pic)
    primary = fmt.primary
    if primary == _DECIMAL:
        return _parse_decimal(s, fmt)
    if primary == _WORDS:
        return _words.parse_words(s.lower())
    if primary == _ROMAN:
        # 'I' matches upper case only; 'i' upper-cases first.
        return parse_roman(s if fmt.case == _UPPER else s.upper())
    if primary == _LETTERS:
        return parse_alpha(s, fmt.case == _UPPER)
    raise RuntimeEvaluationError(
        "D3130", f'$parseInteger: unsupported numbering sequence "{fmt.token}"'
    )


def _parse_decimal(value: str, fmt: _Picture) -> int | float:
    digits = value
    if fmt.ordinal:
        # substring(0, length - 2); the reference clamps a negative end to
        # 0, so a string shorter than the suffix parses as nothing.
        end = len(digits) - 2
        digits = digits[:end] if end > 0 else ""

    if fmt.regular:
        # datetime.js:1109 hard-codes ',' for the regular case, even when
        # the picture's separator is something else.
        digits = digits.replace(",", "")
    else:
        for _position, char in fmt.separators:
            digits = digits.replace(char, "")

    zero_code = fmt.zero_code
    if zero_code != 0x30:
        delta = 0x30 - zero_code
        try:
            digits = "".join([chr(ord(c) + delta) for c in digits])
        except ValueError:
            # The reference applies this offset unguarded and dies with a
            # RangeError out of String.fromCodePoint (datetime.js:1117):
            # any character below the picture's digit family maps to a
            # negative code point. In particular the '-' that
            # $formatInteger itself emits for a negative value does, so
            # the reference cannot read back its own output for a
            # negative value under a non-ASCII digit family. Same
            # failure, with a diagnostic instead of a leaked chr() error.
            raise RuntimeEvaluationError(
                None,
                "$parseInteger: input contains a character outside the picture's "
                f'digit family: "{value}"',
            ) from None

    return _js_parse_int(digits)


def _js_parse_int(s: str) -> int | float:
    """ECMA-262 `parseInt(s)`: the longest valid prefix wins, no digits at
    all is NaN, and the result is a double -- so a value too wide for one
    is returned as a float, matching the reference's loss of precision."""
    match = _JS_INT.match(s.lstrip())
    if match is None:
        return _NAN
    sign, hex_digits, dec_digits = match.groups()
    value = int(hex_digits, 16) if hex_digits is not None else int(dec_digits)
    if sign == "-":
        value = -value
    if -_MAX_EXACT_DOUBLE <= value <= _MAX_EXACT_DOUBLE:
        return value
    try:
        return float(value)
    except OverflowError:
        return -math.inf if value < 0 else math.inf
