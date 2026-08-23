"""English cardinal/ordinal word conversion for date/time picture-string
components ([Yw], [Dwo], etc.) and the reverse (words -> digits) used when
parsing a timestamp against a word-based picture.

Ported from org.json_kula.jsonata_jvm.runtime.datetime.WordNumbers.

Kept separate from runtime/numeric/english_words.py: that module handles
$formatInteger/$parseInteger's full-range "w"/"W"/"Ww" pictures (values up
to and beyond int64), while this one mirrors the Java date/time package's
own narrower, calendar-scale word tables (year/day/month numbers) and its
distinct words-to-digits reverse-parser used only during picture-string
timestamp preprocessing.
"""

from __future__ import annotations

_UNITS = ["", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine"]
_UNITS_ORD = ["", "first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth", "ninth"]
_TEENS = [
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen",
    "sixteen", "seventeen", "eighteen", "nineteen",
]
_TEENS_ORD = [
    "tenth", "eleventh", "twelfth", "thirteenth", "fourteenth", "fifteenth",
    "sixteenth", "seventeenth", "eighteenth", "nineteenth",
]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_TENS_ORD = [
    "", "", "twentieth", "thirtieth", "fortieth", "fiftieth",
    "sixtieth", "seventieth", "eightieth", "ninetieth",
]

_IRREGULAR_ORDINALS = {1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth", 8: "eighth", 9: "ninth", 12: "twelfth"}


def to_cardinal(n: int) -> str:
    """English cardinal words for n (e.g. 2017 -> "two thousand and seventeen")."""
    if n <= 0:
        return str(n)
    parts: list[str] = []
    if n >= 1000:
        parts.append(_UNITS[n // 1000] + " thousand")
        n %= 1000
        if n > 0:
            parts.append(" and ")
    if n >= 100:
        parts.append(_UNITS[n // 100] + " hundred")
        n %= 100
        if n > 0:
            parts.append(" and ")
    if n >= 20:
        parts.append(_TENS[n // 10])
        ones = n % 10
        if ones > 0:
            parts.append("-" + _UNITS[ones])
    elif n >= 10:
        parts.append(_TEENS[n - 10])
    elif n > 0:
        parts.append(_UNITS[n])
    return "".join(parts)


def to_ordinal(n: int) -> str:
    """English ordinal words for n (e.g. 12 -> "twelfth", 31 -> "thirty-first")."""
    if n <= 0:
        return str(n)
    irregular = _IRREGULAR_ORDINALS.get(n)
    if irregular is not None:
        return irregular
    return _build_ordinal(n)


def _build_ordinal(n: int) -> str:
    parts: list[str] = []
    if n >= 1000:
        parts.append(_UNITS[n // 1000] + " thousand")
        n %= 1000
        if n == 0:
            parts.append("th")
            return "".join(parts)
        parts.append(" and ")
    if n >= 100:
        parts.append(_UNITS[n // 100] + " hundred")
        n %= 100
        if n == 0:
            parts.append("th")
            return "".join(parts)
        parts.append(" and ")
    if n >= 20:
        tens_digit = n // 10
        ones = n % 10
        if ones == 0:
            parts.append(_TENS_ORD[tens_digit])
        else:
            parts.append(_TENS[tens_digit] + "-" + _UNITS_ORD[ones])
    elif n >= 10:
        parts.append(_TEENS_ORD[n - 10])
    elif n > 0:
        parts.append(_UNITS_ORD[n])
    return "".join(parts)


_WORD_TO_VALUE: dict[str, int] = {}
for _w in ("one", "first"):
    _WORD_TO_VALUE[_w] = 1
for _w in ("two", "second"):
    _WORD_TO_VALUE[_w] = 2
for _w in ("three", "third"):
    _WORD_TO_VALUE[_w] = 3
for _w in ("four", "fourth"):
    _WORD_TO_VALUE[_w] = 4
for _w in ("five", "fifth"):
    _WORD_TO_VALUE[_w] = 5
for _w in ("six", "sixth"):
    _WORD_TO_VALUE[_w] = 6
for _w in ("seven", "seventh"):
    _WORD_TO_VALUE[_w] = 7
for _w in ("eight", "eighth"):
    _WORD_TO_VALUE[_w] = 8
for _w in ("nine", "ninth"):
    _WORD_TO_VALUE[_w] = 9
for _w in ("ten", "tenth"):
    _WORD_TO_VALUE[_w] = 10
for _w in ("eleven", "eleventh"):
    _WORD_TO_VALUE[_w] = 11
for _w in ("twelve", "twelfth"):
    _WORD_TO_VALUE[_w] = 12
for _w in ("thirteen", "thirteenth"):
    _WORD_TO_VALUE[_w] = 13
for _w in ("fourteen", "fourteenth"):
    _WORD_TO_VALUE[_w] = 14
for _w in ("fifteen", "fifteenth"):
    _WORD_TO_VALUE[_w] = 15
for _w in ("sixteen", "sixteenth"):
    _WORD_TO_VALUE[_w] = 16
for _w in ("seventeen", "seventeenth"):
    _WORD_TO_VALUE[_w] = 17
for _w in ("eighteen", "eighteenth"):
    _WORD_TO_VALUE[_w] = 18
for _w in ("nineteen", "nineteenth"):
    _WORD_TO_VALUE[_w] = 19
for _w in ("twenty", "twentieth"):
    _WORD_TO_VALUE[_w] = 20
for _w in ("thirty", "thirtieth"):
    _WORD_TO_VALUE[_w] = 30
for _w in ("forty", "fortieth"):
    _WORD_TO_VALUE[_w] = 40
for _w in ("fifty", "fiftieth"):
    _WORD_TO_VALUE[_w] = 50
for _w in ("sixty", "sixtieth"):
    _WORD_TO_VALUE[_w] = 60
for _w in ("seventy", "seventieth"):
    _WORD_TO_VALUE[_w] = 70
for _w in ("eighty", "eightieth"):
    _WORD_TO_VALUE[_w] = 80
for _w in ("ninety", "ninetieth"):
    _WORD_TO_VALUE[_w] = 90


def words_to_digits(input_: str) -> str:
    """Converts English number words to a decimal digit string. Returns the
    original string unchanged when no number words are found. Handles
    cardinals, ordinals ("first" -> 1), hyphenated composites
    ("twenty-one" -> 21), and "and" connectors."""
    normalized = input_.lower().replace(",", "").replace(" and ", " ").replace("-", " ")
    words = normalized.split()

    result = 0
    current = 0
    matched = False

    for word in words:
        if word in ("hundred", "hundredth"):
            current = (current if current != 0 else 1) * 100
            matched = True
        elif word in ("thousand", "thousandth"):
            result += 1000 if current == 0 else current * 1000
            current = 0
            matched = True
        else:
            val = _WORD_TO_VALUE.get(word)
            if val is not None:
                current += val
                matched = True

    result += current
    return str(result) if matched else input_
