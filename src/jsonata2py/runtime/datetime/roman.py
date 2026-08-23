"""Roman-numeral conversion for date/time picture-string components
([YI], [Mi], preprocessing of Roman month tokens, etc.).

Ported from org.json_kula.jsonata_jvm.runtime.datetime.RomanNumerals.

Kept separate from runtime/numeric/integer_picture.py's to_roman/parse_roman:
this module mirrors the Java date/time package's own narrower range (1-3999,
matching a calendar year/month/day) and its strict structural validity
regex (used to distinguish a Roman-numeral token from an ordinary word
during picture-string parsing preprocessing).
"""

from __future__ import annotations

import re

_VALS = [1000, 900, 500, 400, 100, 90, 50, 40, 10, 9, 5, 4, 1]
_SYMS = ["M", "CM", "D", "CD", "C", "XC", "L", "XL", "X", "IX", "V", "IV", "I"]

_VALID_ROMAN = re.compile(r"^M{0,3}(CM|CD|D?C{0,3})(XC|XL|L?X{0,3})(IX|IV|V?I{0,3})$")

_CHAR_VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}


def is_valid(s: str) -> bool:
    """True only for structurally valid Roman-numeral strings (I..MMMCMXCIX)."""
    if not s:
        return False
    upper = s.upper()
    return bool(upper) and _VALID_ROMAN.match(upper) is not None


def to_arabic(roman: str) -> int:
    """Converts a Roman-numeral string (either case) to its Arabic integer value."""
    upper = roman.upper()
    result = 0
    prev = 0
    for c in reversed(upper):
        val = _CHAR_VALUES.get(c, 0)
        if val < prev:
            result -= val
        else:
            result += val
            prev = val
    return result


def to_roman(n: int) -> str:
    """Converts an integer 1..3999 to uppercase Roman numerals; returns the
    decimal string outside that range."""
    if n <= 0 or n > 3999:
        return str(n)
    out: list[str] = []
    for val, sym in zip(_VALS, _SYMS, strict=True):
        while n >= val:
            out.append(sym)
            n -= val
    return "".join(out)
