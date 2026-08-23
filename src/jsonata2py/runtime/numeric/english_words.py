"""Converts between long integers and English number words for
$formatInteger and $parseInteger with pictures "w", "W", "Ww".

Ported from org.json_kula.jsonata_jvm.runtime.numeric.EnglishWords.
"""

from __future__ import annotations

import re

from ...errors import _RuntimeEvaluationError as RuntimeEvaluationError

_ONES = [
    "", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen",
]
_TENS = ["", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]
_MAGNITUDES = [1_000_000_000_000, 1_000_000_000, 1_000_000, 1_000]
_MAG_WORDS = ["trillion", "billion", "million", "thousand"]

_WORD_VALUES: dict[str, int] = {}
for _j, _w in enumerate(
    [
        "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
        "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
        "seventeen", "eighteen", "nineteen",
    ]
):
    _WORD_VALUES[_w] = _j
for _j, _w in enumerate(["twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety"]):
    _WORD_VALUES[_w] = (_j + 2) * 10
_WORD_VALUES["hundred"] = 100
_WORD_VALUES["thousand"] = 1_000
_WORD_VALUES["million"] = 1_000_000
_WORD_VALUES["billion"] = 1_000_000_000
_WORD_VALUES["trillion"] = 1_000_000_000_000
_WORD_VALUES["quadrillion"] = 1_000_000_000_000_000
_WORD_VALUES["quintillion"] = 1_000_000_000_000_000_000

_PARSE_SPLIT = re.compile(r"[\s,\-]+")

_LONG_MAX = 9223372036854775807
_LONG_MIN = -9223372036854775808

# =============================================================================
# Cardinal / ordinal word generation
# =============================================================================


def to_words_double(n: float, ordinal: bool) -> str:
    """English words for a float that may exceed long range. Trillions are
    factored out repeatedly until the remainder fits in a (Python) long:
    e.g. 1e46 -> "ten billion trillion trillion trillion"."""
    if n < 0:
        return "minus " + to_words_double(-n, False)
    if n == 0:
        return "zeroth" if ordinal else "zero"
    if n <= _LONG_MAX:
        return to_words(round(n), ordinal)

    trillion = 1_000_000_000_000.0
    trillion_count = 0
    work = n
    while work >= trillion:
        work = work / trillion
        trillion_count += 1
    base = to_words(round(work), False)
    return base + " trillion" * max(0, trillion_count)


def to_words(n: int, ordinal: bool) -> str:
    """English cardinal or ordinal words for n."""
    if n == 0:
        return "zeroth" if ordinal else "zero"
    if n < 0:
        return "minus " + to_words(-n, ordinal)
    cardinal = _words_below(n)
    return _to_ordinal_word(n, cardinal) if ordinal else cardinal


def _words_below(n: int) -> str:
    if n == 0:
        return ""
    if n < 20:
        return _ONES[n]
    if n < 100:
        t = _TENS[n // 10]
        return t if n % 10 == 0 else f"{t}-{_ONES[n % 10]}"
    if n < 1000:
        h = f"{_ONES[n // 100]} hundred"
        rest = n % 100
        return h if rest == 0 else f"{h} and {_words_below(rest)}"
    for m, magnitude in enumerate(_MAGNITUDES):
        if n >= magnitude:
            hi = n // magnitude
            rest = n % magnitude
            s = f"{_words_below(hi)} {_MAG_WORDS[m]}"
            if rest > 0:
                return f"{s} and {_words_below(rest)}" if rest < 100 else f"{s}, {_words_below(rest)}"
            return s
    return str(n)  # fallback for values above trillion (shouldn't occur within long range)


_ONES_ORDINAL = {
    1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth", 6: "sixth",
    7: "seventh", 8: "eighth", 9: "ninth", 10: "tenth", 11: "eleventh", 12: "twelfth",
    13: "thirteenth", 14: "fourteenth", 15: "fifteenth", 16: "sixteenth",
    17: "seventeenth", 18: "eighteenth", 19: "nineteenth",
}
_TENS_ORDINAL = {
    2: "twentieth", 3: "thirtieth", 4: "fortieth", 5: "fiftieth",
    6: "sixtieth", 7: "seventieth", 8: "eightieth", 9: "ninetieth",
}
_ONES_ORD_SUFFIX = {
    1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth",
    6: "sixth", 7: "seventh", 8: "eighth", 9: "ninth",
}
_TENS_WORD = {
    2: "twenty", 3: "thirty", 4: "forty", 5: "fifty",
    6: "sixty", 7: "seventy", 8: "eighty", 9: "ninety",
}
_MAG_LIST = ["trillion", "billion", "million", "thousand"]


def _to_ordinal_word(n: int, cardinal: str) -> str:
    if n < 20:
        return _ONES_ORDINAL.get(n, cardinal)
    if n < 100:
        if n % 10 == 0:
            return _TENS_ORDINAL.get(n // 10, cardinal)
        ones_digit = n % 10
        tens_digit = n // 10
        ones_ord = _ONES_ORD_SUFFIX.get(ones_digit, "")
        tens_word = _TENS_WORD.get(tens_digit, "")
        return f"{tens_word}-{ones_ord}"
    # n >= 100: append ordinal suffix to the last magnitude name.
    if n % 100 == 0:
        for mag in _MAG_LIST:
            if cardinal.endswith(mag):
                return cardinal[: -len(mag)] + mag + "th"
        # Falls through to "hundredth"
        return cardinal.replace("hundred", "hundredth")
    # Non-round: convert the last 1-99 to ordinal, prefix with the main part.
    last_part = n % 100
    if last_part > 0:
        last_ordinal = _to_ordinal_word(last_part, "")
        main_part = _words_below(n - last_part)
        return f"{main_part} and {last_ordinal}"
    return cardinal + "th"


# =============================================================================
# Title-case
# =============================================================================


def title_case(s: str) -> str:
    """Applies title-case to an English number-word string, keeping "and"
    lowercase."""
    if not s:
        return s
    out: list[str] = []
    cap_next = True
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c in (" ", ",", "-"):
            out.append(c)
            cap_next = True
        elif cap_next:
            if s[i : i + 4].lower() == "and ":
                out.append("a")
            else:
                out.append(c.upper())
            cap_next = False
        else:
            out.append(c)
        i += 1
    return "".join(out)


# =============================================================================
# Parsing English words -> long
# =============================================================================


def parse_words(s: str) -> int | float:
    """Standard stacking algorithm: units accumulate in a sub-total, which
    is multiplied into the running total whenever a magnitude word
    (thousand, million, ...) is encountered. Handles "one million one
    thousand" -> 1,001,000 (each magnitude group independent)."""
    tokens = _PARSE_SPLIT.split(s.lower().strip())
    negative = False
    total = 0
    subtotal = 0
    last_magnitude = 0  # tracks last committed magnitude to detect ascending order

    for tok in tokens:
        if not tok or tok == "and":
            continue
        if tok == "minus":
            negative = True
            continue

        val = _WORD_VALUES.get(tok)
        if val is None:
            raise RuntimeEvaluationError(None, f'$parseInteger: unrecognised word token "{tok}"')

        if val == 100:
            # "hundred" multiplies the current sub-total (or implies 1).
            subtotal = (subtotal if subtotal != 0 else 1) * 100
        elif val >= 1_000:
            # >= (not strictly >, despite the Java source's literal `>`):
            # verified against the official suite -- "ten billion trillion
            # trillion trillion" (repeated identical magnitude words, the
            # exact inverse of to_words_double's "divide by 1e12 three
            # times, append trillion three times" encoding) must keep
            # ascending/multiplying on each repeat, not fall back to
            # descending/additive once val==last_magnitude.
            if last_magnitude > 0 and val >= last_magnitude:
                # Ascending magnitude: "one thousand trillion" -> 1000 x 10^12.
                base = total + subtotal
                total = (base if base != 0 else 1) * val
            else:
                # Descending or first: "one million one thousand" -> 10^6 + 1x10^3.
                contribution = (subtotal if subtotal != 0 else 1) * val
                total = total + contribution
            # Deliberately no range clamp here (unlike the Java source's
            # long-overflow guard): Python ints are arbitrary precision, so
            # we let magnitude words keep compounding exactly (e.g. three
            # consecutive "trillion" tokens -> 1e10*1e12*1e12*1e12) and only
            # decide int-vs-float range at the very end. Clamping mid-loop
            # would silently drop later tokens.
            subtotal = 0
            last_magnitude = val
        else:
            subtotal += val
    total += subtotal
    result = -total if negative else total
    if not (_LONG_MIN <= result <= _LONG_MAX):
        # Out of long range: surface as a float (JS double semantics),
        # matching the JSON test-suite expectation (e.g. 1e+46) rather
        # than an unbounded exact Python int.
        return float(result)
    return result


# =============================================================================
# Ordinal suffix stripping (for $parseInteger with ";o" modifier)
# =============================================================================

_IRREGULAR_ORDINALS = {
    "twelfth": "twelve",
    "fifth": "five",
    "ninth": "nine",
    "first": "one",
    "second": "two",
    "third": "three",
}


def strip_ordinal_suffix(s: str) -> str:
    """Strips the ordinal suffix from a word-form ordinal string."""
    lower = s.lower().strip()
    last_space = lower.rfind(" ")
    last_word = lower[last_space + 1 :] if last_space >= 0 else lower

    if last_word in _IRREGULAR_ORDINALS:
        return _replace_last_word(lower, last_space, _IRREGULAR_ORDINALS[last_word])

    # "twentieth" -> "twenty", "thirtieth" -> "thirty", etc.
    if lower.endswith("ieth"):
        return lower[:-4] + "y"

    # Hyphenated: "thirty-fourth" -> last part after "-" has ordinal suffix
    if "-" in lower:
        parts = lower.split("-")
        last_part = parts[-1]
        if last_part.endswith(("st", "nd", "rd", "th")):
            root = last_part[:-2]
            cardinal = _ordinal_root_to_cardinal(root)
            result = " ".join(parts[:-1])
            if result:
                result += " "
            result += cardinal
            return result

    # Standard suffix stripping
    if lower.endswith(("st", "nd", "rd", "th")):
        return _ordinal_root_to_cardinal(lower[:-2])
    return s


def _replace_last_word(full: str, last_space: int, replacement: str) -> str:
    return full[: last_space + 1] + replacement if last_space >= 0 else replacement


_ORDINAL_ROOT_TO_CARDINAL = {
    "nin": "nine",
    "fif": "five",
    "twelf": "twelve",
    "thi": "three",
    "fir": "one",
    "seco": "two",
    "secon": "two",
}


def _ordinal_root_to_cardinal(root: str) -> str:
    return _ORDINAL_ROOT_TO_CARDINAL.get(root, root)
