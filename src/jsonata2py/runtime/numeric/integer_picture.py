"""Picture-based integer formatting and parsing logic for $formatInteger
and $parseInteger.

Ported from org.json_kula.jsonata_jvm.runtime.numeric.IntegerPicture.

Supported picture strings: decimal patterns (e.g. "#,##0"), w/W/Ww
(English words), I/i (Roman numerals), A/a (alphabetic), and any of the
above with the ";o" ordinal modifier.

Java delegates plain decimal patterns to java.text.DecimalFormat. Since
grouping separators are always stripped from parse input before parsing,
and formatting only ever needs a *count* of mandatory-vs-optional digit
positions (no currency/multiplier/percent features DecimalFormat also
supports), this port implements the narrow subset actually used directly
rather than pulling in a general decimal-format library.
"""

from __future__ import annotations

import re

from ...errors import _RuntimeEvaluationError as RuntimeEvaluationError
from . import english_words as _words

_LONG_MIN = -9223372036854775808
_LONG_MAX = 9223372036854775807

# =============================================================================
# Public entry points
# =============================================================================


def format_large(n: float, pic: str) -> str:
    """Formats a float that exceeds long range. Only word pictures (w, W,
    Ww) are supported; anything else raises because it requires an exact
    integer representation."""
    ordinal = pic.endswith(";o")
    base_pic = pic[:-2] if ordinal else pic
    if base_pic == "w":
        return _words.to_words_double(n, ordinal)
    if base_pic == "W":
        return _words.to_words_double(n, ordinal).upper()
    if base_pic == "Ww":
        return _words.title_case(_words.to_words_double(n, ordinal))
    raise RuntimeEvaluationError(None, f"$formatInteger: value is not representable as an integer: {n}")


_VALID_PIC_CHARS = set("#0,:wWIiAa")


def _is_valid_pic_char(c: str) -> bool:
    if c in _VALID_PIC_CHARS:
        return True
    o = ord(c)
    if 0x0660 <= o <= 0x0669:  # Arabic-Indic digits
        return True
    if 0xFF10 <= o <= 0xFF19:  # Full-width digits
        return True
    return "0" <= c <= "9"


def format(n: int, pic: str) -> str:
    """Formats n using the given JSONata integer picture string."""
    ordinal = pic.endswith(";o")
    base_pic = pic[:-2] if ordinal else pic

    pos_pic = base_pic[: base_pic.index(";")] if ";" in base_pic else base_pic
    for c in pos_pic:
        if not _is_valid_pic_char(c):
            raise RuntimeEvaluationError("D3130", f"$formatInteger: picture string contains invalid character '{c}'")

    if base_pic == "w":
        return _words.to_words(n, ordinal)
    if base_pic == "W":
        return _words.to_words(n, ordinal).upper()
    if base_pic == "Ww":
        return _words.title_case(_words.to_words(n, ordinal))
    if base_pic == "I":
        return to_roman(n).upper()
    if base_pic == "i":
        return to_roman(n).lower()
    if base_pic == "A":
        return to_alpha(n, True)
    if base_pic == "a":
        return to_alpha(n, False)
    return _format_ordinal(n, base_pic) if ordinal else _format_decimal(n, base_pic)


def parse(s: str, pic: str) -> int | float:
    """Parses a string back to an int using the given JSONata integer
    picture string."""
    ordinal = pic.endswith(";o")
    base_pic = pic[:-2] if ordinal else pic

    # Empty input with Roman picture is treated as 0 (spec edge-case)
    if not s and base_pic in ("I", "i"):
        return 0

    # Validate that the picture has at least one digit placeholder or is a named format
    if base_pic in ("I", "i", "A", "a", "w", "W", "Ww"):
        has_valid_format = True
    else:
        has_valid_format = any(c == "0" or c.isdigit() for c in base_pic)
    if not has_valid_format:
        raise RuntimeEvaluationError("D3130", "$parseInteger: unsupported picture string")

    input_ = _words.strip_ordinal_suffix(s) if ordinal else s
    if base_pic in ("w", "W", "Ww"):
        return _words.parse_words(input_)
    if base_pic in ("I", "i"):
        return parse_roman(input_)
    if base_pic in ("A", "a"):
        return parse_alpha(input_)
    return _parse_decimal(input_, base_pic)


# =============================================================================
# Decimal picture formatting
# =============================================================================


def _format_decimal(n: int, pic: str) -> str:
    # Check for Unicode digit placeholders (Arabic-Indic, Full-width)
    has_arabic_indic = False
    has_full_width = False
    has_ascii_digit = False
    unicode_zero = ""

    for c in pic:
        o = ord(c)
        if 0x0660 <= o <= 0x0669:
            has_arabic_indic = True
            unicode_zero = chr(0x0660)
            break
        if 0xFF10 <= o <= 0xFF19:
            has_full_width = True
            unicode_zero = chr(0xFF10)
            break
        if c == "0":
            has_ascii_digit = True

    if (has_arabic_indic or has_full_width) and has_ascii_digit:
        raise RuntimeEvaluationError("D3131", "$formatInteger: picture string contains mixed digit groups")
    if has_arabic_indic and has_full_width:
        raise RuntimeEvaluationError("D3131", "$formatInteger: picture string contains mixed digit groups")

    if has_arabic_indic or has_full_width:
        unicode_digit = chr(ord(unicode_zero) + 1)  # any digit in the family
        ascii_pic = pic.replace(unicode_digit, "0").replace(unicode_zero, "0")
        ascii_result = _format_decimal(n, ascii_pic)
        out = []
        for c in ascii_result:
            if "0" <= c <= "9":
                out.append(chr(ord(unicode_zero) + (ord(c) - ord("0"))))
            else:
                out.append(c)
        return "".join(out)

    # Determine whether custom grouping logic is needed
    needs_custom = ":" in pic

    if not needs_custom and "," in pic:
        sep_count = sum(1 for c in pic if c in (",", ":"))
        if sep_count >= 2:
            groups: list[int] = []
            cur = 0
            for c in pic:
                if c in (",", ":", ";"):
                    if cur > 0:
                        groups.append(cur)
                        cur = 0
                elif c in ("#", "0"):
                    cur += 1
            if cur > 0:
                groups.append(cur)
            if len(groups) >= 2 and groups[0] == groups[1]:
                needs_custom = True

    if needs_custom:
        # Handle the sign separately so grouping logic only sees digits.
        negative = n < 0
        plain = str(-n if negative else n)
        formatted = _apply_custom_grouping(plain, pic)
        return f"-{formatted}" if negative else formatted

    # Standard case: single (or no) grouping separator, regular repeating group.
    pat_chars = []
    for c in pic:
        if c == ";":
            break
        if c in ("#", "0", ","):
            pat_chars.append(c)
    pat_str = "".join(pat_chars) or "0"
    return _format_standard(n, pat_str)


def _format_standard(n: int, pat_str: str) -> str:
    """Mimics java.text.DecimalFormat for a plain integer pattern made only
    of '0'/'#'/',' (rounding mode is irrelevant -- n is always exact)."""
    min_int_digits = sum(1 for c in pat_str if c == "0")
    group_size = 0
    if "," in pat_str:
        last_comma = pat_str.rfind(",")
        group_size = sum(1 for c in pat_str[last_comma + 1 :] if c in ("#", "0"))

    negative = n < 0
    abs_n = -n if negative else n
    if min_int_digits == 0 and abs_n == 0:
        digits = ""
    else:
        digits = str(abs_n)
        if len(digits) < min_int_digits:
            digits = digits.rjust(min_int_digits, "0")

    if group_size > 0 and len(digits) > group_size:
        parts: list[str] = []
        i = len(digits)
        while i > group_size:
            parts.insert(0, digits[i - group_size : i])
            i -= group_size
        parts.insert(0, digits[:i])
        digits = ",".join(parts)

    return f"-{digits}" if negative else digits


def _apply_custom_grouping(plain: str, pic: str) -> str:
    """Applies custom grouping separators to a string of digits (no sign).
    Supports both ',' and ':' as separator characters."""
    first_sep_idx = -1
    first_sep = ","
    leading_placeholders = 0
    pre_sep_digits = 0

    for i, c in enumerate(pic):
        if c == ";":
            break
        if c in (":", ","):
            first_sep_idx = i
            first_sep = c
            leading_placeholders = pre_sep_digits
            break
        if c in ("#", "0"):
            pre_sep_digits += 1

    if first_sep_idx < 0:
        return plain

    # Count digit groups after the first separator (right-to-left).
    right_groups: list[int] = []
    cnt = 0
    for i in range(len(pic) - 1, first_sep_idx, -1):
        c = pic[i]
        if c in ("#", "0"):
            cnt += 1
        elif c in (",", ":") and cnt > 0:
            right_groups.append(cnt)
            cnt = 0
    if cnt > 0:
        right_groups.append(cnt)
    right_groups.reverse()

    fixed_size = sum(right_groups)
    leading_digits = max(leading_placeholders, len(plain) - fixed_size)
    leading_digits = min(leading_digits, len(plain))

    result = [plain[:leading_digits]]
    pos = leading_digits
    for gi, size in enumerate(right_groups):
        if pos >= len(plain):
            break
        result.append(first_sep if gi == 0 else ",")
        result.append(plain[pos : min(pos + size, len(plain))])
        pos += size
    if pos < len(plain):
        result.append(",")
        result.append(plain[pos:])
    return "".join(result)


def _format_ordinal(n: int, pic: str) -> str:
    return _format_decimal(n, pic) + ordinal_suffix(n)


def ordinal_suffix(n: int) -> str:
    """Returns the ordinal suffix (st, nd, rd, th) for n. Uses absolute
    value of the modulus so negative numbers work correctly (e.g. -1 ->
    "st" not "th")."""
    last_two = abs(n % 100)
    last_one = abs(n % 10)
    if 11 <= last_two <= 13:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(last_one, "th")


# =============================================================================
# Roman numerals (extended: 1 - 3,999,999)
# =============================================================================

_ROMAN_VALS = [1000, 900, 500, 400, 100, 90, 50, 40, 10, 9, 5, 4, 1]
_ROMAN_SYMS = ["M", "CM", "D", "CD", "C", "XC", "L", "XL", "X", "IX", "V", "IV", "I"]


def to_roman(n: int) -> str:
    """Converts n to a Roman numeral string. Supports 1 through 3,999,999;
    zero and negatives raise."""
    if n == 0:
        return ""
    if n < 0 or n > 3_999_999:
        raise RuntimeEvaluationError(None, "$formatInteger: Roman numerals are only supported for 1-3,999,999")
    out = []
    for val, sym in zip(_ROMAN_VALS, _ROMAN_SYMS, strict=True):
        while n >= val:
            out.append(sym)
            n -= val
    return "".join(out)


_ROMAN_VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}


def parse_roman(s: str) -> int:
    """Parses a Roman numeral string (case-insensitive) to an int."""
    if not s:
        return 0
    s = s.upper().strip()
    result = 0
    prev = 0
    for c in reversed(s):
        cv = _ROMAN_VALUES.get(c, -1)
        if cv < 0:
            raise RuntimeEvaluationError(None, f"$parseInteger: invalid Roman numeral character '{c}'")
        result += -cv if cv < prev else cv
        prev = cv
    return result


# =============================================================================
# Alphabetic (A, B ... Z, AA, AB ...)
# =============================================================================


def to_alpha(n: int, upper: bool) -> str:
    """Converts n (1-based) to an alphabetic label."""
    if n <= 0:
        raise RuntimeEvaluationError(None, "$formatInteger: alphabetic format requires a positive integer")
    base = ord("A") if upper else ord("a")
    out: list[str] = []
    while n > 0:
        n -= 1
        out.insert(0, chr(base + n % 26))
        n //= 26
    return "".join(out)


def parse_alpha(s: str) -> int:
    """Parses an alphabetic label back to a 1-based int."""
    s = s.upper().strip()
    result = 0
    for c in s:
        if c < "A" or c > "Z":
            raise RuntimeEvaluationError(None, f"$parseInteger: invalid alphabetic character '{c}'")
        result = result * 26 + (ord(c) - ord("A") + 1)
    return result


# =============================================================================
# Decimal picture parsing
# =============================================================================

_DECIMAL_RE = re.compile(r"-?\d+")


def _parse_decimal(s: str, pic: str) -> int:
    zero_digit = _find_zero_digit(pic)
    normalized = _normalize_unicode_digits(s, zero_digit)

    seps = _extract_grouping_separators(pic)
    stripped = normalized
    for sep in seps:
        stripped = stripped.replace(sep, "")
    stripped = stripped.strip()

    if not _DECIMAL_RE.fullmatch(stripped):
        raise RuntimeEvaluationError(None, f'$parseInteger: cannot parse "{s}" with picture "{pic}"')
    return int(stripped)


def _find_zero_digit(pic: str) -> str:
    for c in pic:
        if c.isdigit():
            val = int(c) if c in "0123456789" else None
            if val is None:
                # Unicode digit: derive the numeric value then back out zero.
                try:
                    val = int(c)
                except ValueError:
                    continue
            if 0 <= val <= 9:
                return chr(ord(c) - val)
    return "0"


def _normalize_unicode_digits(input_: str, zero_digit: str) -> str:
    if zero_digit == "0":
        return input_
    out = []
    for c in input_:
        if c.isdigit():
            try:
                val = int(c)
            except ValueError:
                val = None
            if val is not None and 0 <= val <= 9:
                out.append(chr(ord("0") + val))
            else:
                out.append(c)
        else:
            out.append(c)
    return "".join(out)


def _extract_grouping_separators(pic: str) -> str:
    return "".join(c for c in pic if not c.isdigit() and c not in ("#", "0"))
