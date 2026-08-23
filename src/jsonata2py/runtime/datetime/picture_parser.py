"""Parses a timestamp string using an XPath/XQuery picture string and
returns milliseconds since the Unix epoch, for $toMillis with a picture arg.

Ported from org.json_kula.jsonata_jvm.runtime.datetime.PictureParser.

Java's real implementation delegates to java.time.format.DateTimeFormatterBuilder
(parseCaseInsensitive + parseLenient, with automatic "adjacent value parsing"
merging consecutive numeric fields with no literal between them, and
appendText(field) silently falling back to numeric parsing when the input
isn't a recognised name). There is no equivalent library in Python, so this
port builds a single case-insensitive regex over the picture's tokens
directly, replicating that adjacent-value-parsing rule by hand (all but the
last field in a run of consecutive field components get an *exact*-width
digit group; only the last field in a run is greedy/variable-width) and
replicating the appendText behaviour by pre-converting every word/Roman/
letter representation to plain digits during preprocessing, so the field
regexes below only ever need to be numeric, or (for [F]/[MNn]/[P]) a plain
case-insensitive name/token alternation.
"""

from __future__ import annotations

import contextlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import date as _date
from datetime import timezone as _tz
from typing import Any

from ...errors import _RuntimeEvaluationError as RuntimeEvaluationError
from .. import context as _ctx
from . import roman as _roman
from . import word_numbers as _words
from .picture_formatter import _DAY_NAMES_FULL, _DAY_NAMES_SHORT, _MONTH_NAMES_FULL, _MONTH_NAMES_SHORT, check_brackets

_GMT_PATTERN = re.compile(r"GMT([+-])(\d{1,2})(?::(\d{2}))?")
_BARE_OFFSET = re.compile(r" ([+-])(\d{2})(\d{2})$")
_ORDINAL_TAIL = re.compile(r"(\d)(st|nd|rd|th)")
_MONTH_NAMES_LOWER = [m.lower() for m in _MONTH_NAMES_FULL]

_MONTH_ABBREV_TO_NUMBER = {
    "JA": 1, "JANUARY": 1, "FE": 2, "FEBRUARY": 2,
    "MA": 3, "MAR": 3, "MARCH": 3, "AP": 4, "APRIL": 4,
    "MY": 5, "MAY": 5, "JN": 6, "JUNE": 6,
    "JL": 7, "JULY": 7, "AU": 8, "AUGUST": 8,
    "SE": 9, "SEPTEMBER": 9, "OC": 10, "OCTOBER": 10,
    "NO": 11, "NOVEMBER": 11, "DE": 12, "DECEMBER": 12,
    "C": 3,
}
_MONTH_ABBREVS = {"C", "JA", "FE", "MA", "AP", "MY", "JN", "JL", "AU", "SE", "OC", "NO", "DE"}


# =============================================================================
# Entry point
# =============================================================================


def parse(timestamp: str, picture: str) -> int | None:
    """Returns epoch millis, or None when the input does not match the
    picture (JSONata's "undefined result" for $toMillis)."""
    check_brackets(picture)

    _compute_day_words_converted(picture)
    processed = _preprocess(timestamp, picture)
    regex, field_order = _build_regex(picture)  # may raise D3132/D3133/D3136

    m = regex.fullmatch(processed)
    if m is None:
        return None

    fields = _extract_fields(m, field_order)

    lower_pic = picture.lower()
    if ("[dwo]" in lower_pic or "[dwwo]" in lower_pic) and "day_of_year" in fields:
        year = fields.get("year", 1)
        date = _date(year, 1, 1) + timedelta(days=fields["day_of_year"] - 1)
        naive = datetime(date.year, date.month, date.day, tzinfo=UTC)
        return round(naive.timestamp() * 1000)

    return _reconstruct_millis(fields, picture)


# =============================================================================
# Manual reconstruction
# =============================================================================


def _reconstruct_millis(fields: dict[str, Any], picture: str) -> int | None:
    has_iso_week_year = "[X]" in picture or "[x]" in picture
    has_iso_week = "[W]" in picture
    if has_iso_week_year or has_iso_week:
        raise RuntimeEvaluationError("D3136", "Date/time underspecified")

    has_hours = "[h" in picture or "[H]" in picture
    has_minutes = "[m]" in picture
    has_seconds = "[s]" in picture
    if (has_minutes or has_seconds) and not has_hours:
        raise RuntimeEvaluationError("D3136", "Date/time underspecified")

    has_year = "[Y" in picture
    has_month = re.search(r"\[M(?!m)[^\]]*\]", picture) is not None
    has_day_month = "[D]" in picture
    has_day_year = "[d]" in picture.lower() and "[D]" not in picture
    if has_year and has_day_month and not has_month and not has_day_year:
        raise RuntimeEvaluationError("D3136", "Date/time underspecified")

    picture_has_date = (
        ("[Y" in picture and "]" in picture)
        or ("[M" in picture and "[m" not in picture and "[MA]" not in picture)
        or "[d" in picture.lower()
        or "[F]" in picture
    )
    lp = picture.lower()
    picture_has_time = "[h" in lp or "[m]" in lp or "[s]" in lp

    hour = fields.get("hour24")
    if hour is None:
        h12 = fields.get("hour12")
        if h12 is not None:
            pm = bool(fields.get("pm", False))
            h12 %= 12
            hour = h12 + (12 if pm else 0)
        else:
            hour = 0
    minute = fields.get("minute", 0)
    second = fields.get("second", 0)
    millis = fields.get("millis", 0)
    zone = fields.get("tz", UTC)

    if not picture_has_date:
        if not picture_has_time:
            return None
        # "Today" comes from the evaluation clock, not the wall clock: $now
        # and $millis are deliberately frozen at the start of an evaluation,
        # so reading datetime.now() here could put a time-only parse on a
        # different date than $now reports when an evaluation straddles
        # midnight. Falls back to wall-clock time outside an evaluation.
        today = datetime.fromtimestamp(_ctx.evaluation_millis() / 1000.0, UTC).date()
        naive = datetime(today.year, today.month, today.day, hour, minute, second, millis * 1000)
        return _to_epoch_millis(naive, zone)

    year = fields.get("year", 1)
    month = fields.get("month", 1)
    day_of_month = fields.get("day_of_month", 1)
    day_of_year = fields.get("day_of_year", -1)

    if day_of_year > 0:
        date = _date(year, 1, 1) + timedelta(days=day_of_year - 1)
    else:
        date = _date(year, month, day_of_month)

    naive = datetime(date.year, date.month, date.day, hour, minute, second, millis * 1000)
    return _to_epoch_millis(naive, zone)


def _to_epoch_millis(naive: datetime, zone: _tz) -> int:
    aware = naive.replace(tzinfo=zone)
    return round(aware.timestamp() * 1000)


# =============================================================================
# Preprocessing: convert non-numeric representations to numbers
# =============================================================================


def _preprocess(timestamp: str, picture: str) -> str:
    result = _strip_gmt(timestamp)
    result = _normalize_offset(result)
    if "[D" in picture and "o" in picture:
        result = _ORDINAL_TAIL.sub(r"\1", result)
    if "[Mi]" in picture:
        result = _convert_roman_month(result)
    if "[MA]" in picture and "[M01]" not in picture:
        result = _convert_month_letters(result)

    needs_day_words = _needs_day_word_conversion(picture)
    if needs_day_words:
        result = _convert_day_words(result, picture)

    if _has_year_words_or_roman(picture):
        result = _convert_year_part(result, picture, needs_day_words)

    if "[DW]" not in picture:
        parts = result.split()
        if parts and len(parts[0]) <= 2 and not parts[0].isdigit():
            up = parts[0].upper()
            if up not in _MONTH_ABBREVS:
                n = _letter_to_day(parts[0])
                if n > 0:
                    parts[0] = str(n)
                    result = " ".join(parts)
    return result


def _strip_gmt(s: str) -> str:
    if "GMT" not in s:
        return s

    def _sub(m: re.Match[str]) -> str:
        sign = m.group(1)
        hours = m.group(2).zfill(2)
        mins = m.group(3) if m.group(3) is not None else "00"
        return f"{sign}{hours}:{mins}"

    r = _GMT_PATTERN.sub(_sub, s)
    r = r.replace(" GMT", "").replace("GMT", "+00:00")
    return r


def _normalize_offset(s: str) -> str:
    m = _BARE_OFFSET.search(s)
    if m:
        return _BARE_OFFSET.sub(r" \1\2:\3", s)
    return s


def _convert_roman_month(input_: str) -> str:
    parts = input_.split()
    out = []
    for p in parts:
        upper = p.upper()
        out.append(str(_roman.to_arabic(upper)) if _roman.is_valid(upper) else p)
    return " ".join(out)


def _convert_month_letters(input_: str) -> str:
    parts = input_.split()
    out = []
    for p in parts:
        n = _MONTH_ABBREV_TO_NUMBER.get(p.upper())
        out.append(f"{n:02d}" if n is not None else p)
    return " ".join(out)


def _needs_day_word_conversion(picture: str) -> bool:
    lower = picture.lower()
    has_dwwo = "[DWwo]" in picture
    has_dw = re.search(r"\[DW\]", picture) is not None
    has_dwo = "[dwo]" in lower or "[dwwo]" in lower
    has_dw_lower = re.search(r"\[dw\]", lower) is not None
    return has_dwwo or has_dwo or has_dw_lower or (has_dw and "[yw]" in lower)


def _compute_day_words_converted(picture: str) -> bool:
    return (
        "[DWwo]" in picture
        or "[dwo" in picture.lower()
        or (re.search(r"\[DW\]", picture) is not None and "[yw]" in picture.lower())
    )


def _has_year_words_or_roman(picture: str) -> bool:
    lower = picture.lower()
    return (
        "[yw]" in lower
        or "[ywo" in lower
        or "[yi]" in lower
        or "[YI]" in picture
        or ("[Y]" in picture and "[Yw]" not in picture)
    )


def _convert_day_words(input_: str, picture: str) -> str:
    words = input_.split()
    sb: list[str] = []
    has_dwwo = "[DWwo]" in picture

    def sb_str() -> str:
        return "".join(sb)

    i = 0
    n = len(words)
    while i < n:
        word = words[i]
        clean = word.replace(",", "").replace(".", "")
        lower = clean.lower()

        if i == 0 and has_dwwo:
            day_abbrs = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
            is_day = any(lower == d or lower.startswith(d + ",") or lower.startswith(d + ".") for d in day_abbrs)
            if is_day:
                sb.append(word)
                i += 1
                continue

        if has_dwwo and sb:
            converted = _words.words_to_digits(clean)
            if converted != clean:
                sb.append(" ")
                sb.append(converted)
                for j in range(i + 1, n):
                    sb.append(" ")
                    sb.append(words[j])
                break

        if lower in ("day", "of"):
            for j in range(i, n):
                if sb and not sb_str().endswith(" "):
                    sb.append(" ")
                sb.append(words[j])
            break

        if not has_dwwo:
            is_month = any(lower.startswith(m[:3]) for m in _MONTH_NAMES_LOWER)
            if is_month:
                for j in range(i, n):
                    if sb and not sb_str().endswith(" "):
                        sb.append(" ")
                    sb.append(words[j])
                break

        test = " ".join(w.replace(",", "").replace(".", "") for w in words[: i + 1])
        converted = _words.words_to_digits(test)
        if converted != test:
            sb = [converted]
        elif clean.isdigit():
            if sb and not sb_str().endswith(" "):
                sb.append(" ")
            sb.append(word)
        else:
            if sb and not sb_str().endswith(" "):
                sb.append(" ")
            sb.append(word)
        i += 1

    return sb_str().strip()


def _convert_year_part(input_: str, picture: str, day_words_converted: bool) -> str:
    lower = picture.lower()

    if "i]" in lower or "[YI]" in picture:
        input_ = _convert_roman_tokens(input_)

    has_dw_for_year = (
        (day_words_converted and "[dwo]" in lower and "[Y]" in picture and "[Yw]" not in picture)
        or (day_words_converted and "[dw]" in lower and "[yw]" in lower)
        or (day_words_converted and "[DW]" in picture and "[yw]" in lower)
    )

    if has_dw_for_year:
        parts = input_.split()
        month_idx = -1
        year_start = -1
        for i, p in enumerate(parts):
            lp = p.lower()
            for m in _MONTH_NAMES_LOWER:
                if lp.startswith(m[:3]):
                    month_idx = i
                    break
            if lp == "day" and i + 2 < len(parts):
                year_start = i + 2
                break
            if month_idx >= 0:
                break

        if 0 <= month_idx < len(parts) - 1:
            year_words = " ".join(parts[month_idx + 1 :])
            converted_year = _words.words_to_digits(year_words)
            if converted_year != year_words:
                month_clean = parts[month_idx].replace(",", "")
                has_comma = "," in parts[month_idx]
                prefix = "".join(parts[pi] + " " for pi in range(1, month_idx))
                input_ = parts[0] + " " + prefix + month_clean + (", " if has_comma else " ") + converted_year
        elif 0 <= year_start < len(parts):
            year_words = " ".join(parts[year_start:])
            converted_year = _words.words_to_digits(year_words)
            if converted_year != year_words:
                nb = " ".join(parts[: year_start - 1])
                input_ = (nb + " " + converted_year) if nb else converted_year
    else:
        has_dw_in_pic = "[dw]" in lower
        has_yw_in_pic = "[yw]" in lower
        if not (has_dw_in_pic and has_yw_in_pic):
            converted = _words.words_to_digits(input_)
            if converted != input_:
                input_ = converted

    return input_


def _convert_roman_tokens(input_: str) -> str:
    parts = input_.split()
    out = []
    for p in parts:
        if _roman.is_valid(p) or _roman.is_valid(p.upper()):
            out.append(str(_roman.to_arabic(p.upper())))
        else:
            out.append(p)
    return " ".join(out)


def _letter_to_day(letter: str) -> int:
    day = 0
    for c in letter.upper():
        if "A" <= c <= "Z":
            day = day * 26 + (ord(c) - ord("A") + 1)
    return day


# =============================================================================
# Regex-based formatter (replaces DateTimeFormatterBuilder)
# =============================================================================


@dataclass
class _FieldSpec:
    kind: str
    regex_exact: str
    regex_greedy: str
    decode: Callable[[str], Any]


def _digit_width(mod: str) -> int:
    width = sum(1 for c in mod if c.isdigit())
    flex = "*" in mod
    min_width = width if width > 0 else (1 if flex else 0)
    if flex and "-" in mod:
        parts = mod.split("-")
        if len(parts) > 1 and parts[1]:
            with contextlib.suppress(ValueError):
                min_width = int(re.sub(r"[^0-9].*", "", parts[1]) or "0") or min_width
    return min_width


def _text_or_numeric_decode(lookup: dict[str, int]) -> Callable[[str], int]:
    # Mirrors java.time's appendText(field) (single-arg form): when the
    # captured text isn't one of the known names, it falls back to a plain
    # numeric value instead of failing to parse (see e.g. "[F1]" matching
    # a bare digit for day-of-week).
    def decode(s: str) -> int:
        if s.isdigit():
            return int(s)
        return lookup[s.lower()]

    return decode


def _month_name_spec() -> _FieldSpec:
    pairs = list(zip(_MONTH_NAMES_FULL, range(1, 13), strict=True)) + list(zip(_MONTH_NAMES_SHORT, range(1, 13), strict=True))
    names = sorted({n for n, _ in pairs}, key=len, reverse=True)
    frag = "(?:" + "|".join(re.escape(n) for n in names) + r"|\d+)"
    lookup = {n.lower(): v for n, v in pairs}
    return _FieldSpec("month", frag, frag, _text_or_numeric_decode(lookup))


def _day_name_spec() -> _FieldSpec:
    pairs = list(zip(_DAY_NAMES_FULL, range(1, 8), strict=True)) + list(zip(_DAY_NAMES_SHORT, range(1, 8), strict=True))
    names = sorted({n for n, _ in pairs}, key=len, reverse=True)
    frag = "(?:" + "|".join(re.escape(n) for n in names) + r"|\d+)"
    lookup = {n.lower(): v for n, v in pairs}
    return _FieldSpec("day_name", frag, frag, _text_or_numeric_decode(lookup))


def _decode_fraction(s: str) -> int:
    return int(s.ljust(3, "0")[:3])


def _decode_tz(s: str) -> _tz:
    if s == "Z":
        return UTC
    sign = 1 if s[0] == "+" else -1
    h = int(s[1:3])
    mi = int(s[4:6])
    return _tz(sign * timedelta(hours=h, minutes=mi))


def _field_spec(d: str, mod: str) -> _FieldSpec:
    if d == "Y":
        if mod in ("N", "n"):
            raise RuntimeEvaluationError("D3133", "Year name component is not supported")
        w = _digit_width(mod) or 1
        return _FieldSpec("year", rf"-?\d{{{w}}}", r"-?\d+", int)
    if d == "M":
        if mod and mod[0] in ("N", "n"):
            return _month_name_spec()
        w = _digit_width(mod) or 2
        return _FieldSpec("month", rf"\d{{{w}}}", r"\d+", int)
    if d == "D":
        w = _digit_width(mod) or 2
        return _FieldSpec("day_of_month", rf"\d{{{w}}}", r"\d+", int)
    if d == "d":
        w = _digit_width(mod) or 3
        return _FieldSpec("day_of_year", rf"\d{{{w}}}", r"\d+", int)
    if d == "X":
        w = _digit_width(mod) or 4
        return _FieldSpec("iso_week_year", rf"\d{{{w}}}", r"\d+", int)
    if d == "W":
        w = _digit_width(mod) or 2
        return _FieldSpec("iso_week", rf"\d{{{w}}}", r"\d+", int)
    if d in ("x", "w"):
        raise RuntimeEvaluationError("D3136", "Date/time underspecified")
    if d == "H":
        w = _digit_width(mod) or 2
        return _FieldSpec("hour24", rf"\d{{{w}}}", r"\d+", int)
    if d == "h":
        w = 1 if mod.startswith("#") else max(1, _digit_width(mod))
        return _FieldSpec("hour12", rf"\d{{{w}}}", r"\d+", int)
    if d == "m":
        w = _digit_width(mod) or 2
        return _FieldSpec("minute", rf"\d{{{w}}}", r"\d+", int)
    if d == "s":
        w = _digit_width(mod) or 2
        return _FieldSpec("second", rf"\d{{{w}}}", r"\d+", int)
    if d == "f":
        width = sum(1 for c in mod if c.isdigit())
        frag = rf"\d{{{width}}}" if width > 0 else r"\d{1,3}"
        return _FieldSpec("millis", frag, frag, _decode_fraction)
    if d == "P":
        frag = r"(?:am|pm)"
        return _FieldSpec("ampm", frag, frag, lambda s: s.lower() == "pm")
    if d in ("Z", "z"):
        frag = r"(?:Z|[+-]\d{2}:\d{2})"
        return _FieldSpec("tz", frag, frag, _decode_tz)
    if d == "F":
        return _day_name_spec()
    if d in ("C", "E"):
        frag = "(?:ISO)"
        return _FieldSpec("literal_iso", frag, frag, lambda s: None)
    raise RuntimeEvaluationError("D3132", f"Unknown picture-string component: [{d}{mod}]")


def _tokenize(picture: str) -> list[tuple[str, str] | tuple[str, str, str]]:
    tokens: list[tuple[str, str] | tuple[str, str, str]] = []
    buf: list[str] = []
    i, n = 0, len(picture)
    while i < n:
        c = picture[i]
        if c == "[" and i + 1 < n and picture[i + 1] == "[":
            buf.append("[")
            i += 2
            continue
        if c == "[":
            j = picture.find("]", i + 1)
            if j < 0:
                raise RuntimeEvaluationError("D3135", "Unclosed '[' in picture string")
            if buf:
                tokens.append(("lit", "".join(buf)))
                buf.clear()
            spec = re.sub(r"\s+", "", picture[i + 1 : j])
            d = spec[0] if spec else ""
            mod = spec[1:] if spec else ""
            tokens.append(("field", d, mod))
            i = j + 1
            continue
        if c == "]" and i + 1 < n and picture[i + 1] == "]":
            buf.append("]")
            i += 2
            continue
        buf.append(c)
        i += 1
    if buf:
        tokens.append(("lit", "".join(buf)))
    return tokens


def _build_regex(picture: str) -> tuple[re.Pattern[str], list[tuple[str, _FieldSpec]]]:
    tokens = _tokenize(picture)
    specs: list[_FieldSpec | None] = [
        _field_spec(tok[1], tok[2]) if len(tok) == 3 and tok[0] == "field" else None for tok in tokens
    ]

    parts: list[str] = []
    field_order: list[tuple[str, _FieldSpec]] = []
    counter = 0
    n = len(tokens)
    i = 0
    while i < n:
        if tokens[i][0] == "lit":
            parts.append(re.escape(tokens[i][1]))
            i += 1
            continue
        j = i
        while j < n and tokens[j][0] == "field":
            j += 1
        run = list(range(i, j))
        for k, idx in enumerate(run):
            spec = specs[idx]
            assert spec is not None
            is_last = k == len(run) - 1
            gname = f"g{counter}"
            counter += 1
            frag = spec.regex_greedy if is_last else spec.regex_exact
            parts.append(f"(?P<{gname}>{frag})")
            field_order.append((gname, spec))
        i = j

    pattern = "".join(parts)
    return re.compile(pattern, re.IGNORECASE), field_order


def _extract_fields(m: re.Match[str], field_order: list[tuple[str, _FieldSpec]]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    for gname, spec in field_order:
        text = m.group(gname)
        if text is None:
            continue
        value = spec.decode(text)
        if spec.kind == "ampm":
            fields["pm"] = value
        elif spec.kind == "tz":
            fields["tz"] = value
        elif spec.kind in ("day_name", "literal_iso"):
            continue
        else:
            fields[spec.kind] = value
    return fields
