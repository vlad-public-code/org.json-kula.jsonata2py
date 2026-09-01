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
from functools import lru_cache
from typing import Any, NamedTuple

from ...errors import _RuntimeEvaluationError as RuntimeEvaluationError
from .. import context as _ctx
from . import epoch as _epoch
from . import roman as _roman
from . import word_numbers as _words
from .picture_formatter import _DAY_NAMES_FULL, _DAY_NAMES_SHORT, _MONTH_NAMES_FULL, _MONTH_NAMES_SHORT, check_brackets

# A picture string (and therefore any modifier sliced out of it) can come
# from input data -- `$fromMillis(t, doc.pic)` -- so its length is
# caller-controlled. lru_cache's maxsize bounds the ENTRY COUNT, not the
# memory those entries retain: measured on an equivalent cache, 60 distinct
# 200 KB pictures held 12.3 MB, and a full 256-entry cache of them ~51 MB.
# A real picture is tens of characters, so anything longer is simply not
# cached -- identical result, slower, O(1) memory.
_MAX_CACHEABLE_PICTURE = 256

# Separate, far looser bound on the COMPILED pattern a picture expands to.
# See _uncached__build_regex: this is the one place a picture escapes into
# the stdlib's own regex cache, which bounds entries but not their size.
_MAX_REGEX_CHARS = 8192

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


class _PicInfo(NamedTuple):
    """Everything `parse` needs that depends only on the picture string.

    Each field is a bool, so the whole thing is immutable and holds no
    per-evaluation state (no timezone, no clock snapshot, no bindings): the
    parsed timestamp, the zone and "today" are all resolved per call, further
    down in `_reconstruct_millis`.
    """

    # _preprocess
    strip_ordinal_tail: bool
    roman_month: bool
    month_letters: bool
    needs_day_words: bool
    year_words_or_roman: bool
    convert_leading_day_letter: bool
    # parse
    day_of_year_in_words: bool
    # _reconstruct_millis
    iso_week_component: bool
    time_without_hours: bool
    day_without_month: bool
    has_date: bool
    has_time: bool


def _picture_info(picture: str) -> _PicInfo:
    if len(picture) > _MAX_CACHEABLE_PICTURE:
        return _uncached__picture_info(picture)
    return _cached__picture_info(picture)


@lru_cache(maxsize=256)
def _cached__picture_info(picture: str) -> _PicInfo:
    return _uncached__picture_info(picture)


def _uncached__picture_info(picture: str) -> _PicInfo:
    """The ~20 substring and regex probes `parse` makes against the picture,
    hoisted out of the per-call path.

    Memoized because the picture is a compile-time literal at essentially
    every `$toMillis` call site; jsonata2js re-derives the equivalent state on
    every call (`datetime.js:884` says so: `// TODO can cache this against the
    picture`). D3135 escapes the cache, since `lru_cache` does not memoise
    exceptions -- which is what we want: it stays raised on every call.

    Nothing that can raise a *later* error code is hoisted in here: D3132 and
    D3133 stay in `_build_regex` and D3136 stays in `_reconstruct_millis`, so
    the order in which competing errors surface is unchanged.
    """
    check_brackets(picture)
    lower = picture.lower()
    return _PicInfo(
        strip_ordinal_tail="[D" in picture and "o" in picture,
        roman_month="[Mi]" in picture,
        month_letters="[MA]" in picture and "[M01]" not in picture,
        needs_day_words=_needs_day_word_conversion(picture),
        year_words_or_roman=_has_year_words_or_roman(picture),
        convert_leading_day_letter="[DW]" not in picture,
        day_of_year_in_words="[dwo]" in lower or "[dwwo]" in lower,
        iso_week_component="[X]" in picture or "[x]" in picture or "[W]" in picture,
        time_without_hours=("[m]" in picture or "[s]" in picture)
        and not ("[h" in picture or "[H]" in picture),
        day_without_month="[Y" in picture
        and "[D]" in picture
        and re.search(r"\[M(?!m)[^\]]*\]", picture) is None
        and not ("[d]" in lower and "[D]" not in picture),
        has_date=("[Y" in picture and "]" in picture)
        or ("[M" in picture and "[m" not in picture and "[MA]" not in picture)
        or "[d" in lower
        or "[F]" in picture,
        has_time="[h" in lower or "[m]" in lower or "[s]" in lower,
    )


def parse(timestamp: str, picture: str) -> int | None:
    """Returns epoch millis, or None when the input does not match the
    picture (JSONata's "undefined result" for $toMillis)."""
    info = _picture_info(picture)

    _compute_day_words_converted(picture)
    processed = _preprocess(timestamp, picture, info)
    regex, field_order = _build_regex(picture)  # may raise D3132/D3133/D3136

    m = regex.fullmatch(processed)
    if m is None:
        return None

    fields = _extract_fields(m, field_order)

    if info.day_of_year_in_words and "day_of_year" in fields:
        year = fields.get("year", 1)
        date = _date(year, 1, 1) + timedelta(days=fields["day_of_year"] - 1)
        naive = datetime(date.year, date.month, date.day, tzinfo=UTC)
        return _epoch.to_millis(naive)

    return _reconstruct_millis(fields, info)


# =============================================================================
# Manual reconstruction
# =============================================================================


def _reconstruct_millis(fields: dict[str, Any], info: _PicInfo) -> int | None:
    # The three D3136 rules stay here rather than in `_picture_info`: they must
    # only fire once the input has actually matched the picture.
    if info.iso_week_component or info.time_without_hours or info.day_without_month:
        raise RuntimeEvaluationError("D3136", "Date/time underspecified")

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

    if not info.has_date:
        if not info.has_time:
            return None
        # "Today" comes from the evaluation clock, not the wall clock: $now
        # and $millis are deliberately frozen at the start of an evaluation,
        # so reading datetime.now() here could put a time-only parse on a
        # different date than $now reports when an evaluation straddles
        # midnight. Falls back to wall-clock time outside an evaluation.
        today = _epoch.to_datetime(_ctx.evaluation_millis()).date()
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
    # _epoch, not datetime.timestamp(): the platform routine behind it
    # rejects pre-1970 instants on Windows.
    return _epoch.to_millis(naive.replace(tzinfo=zone))


# =============================================================================
# Preprocessing: convert non-numeric representations to numbers
# =============================================================================


def _preprocess(timestamp: str, picture: str, info: _PicInfo) -> str:
    result = _strip_gmt(timestamp)
    result = _normalize_offset(result)
    if info.strip_ordinal_tail:
        result = _ORDINAL_TAIL.sub(r"\1", result)
    if info.roman_month:
        result = _convert_roman_month(result)
    if info.month_letters:
        result = _convert_month_letters(result)

    needs_day_words = info.needs_day_words
    if needs_day_words:
        result = _convert_day_words(result, picture)

    if info.year_words_or_roman:
        result = _convert_year_part(result, picture, needs_day_words)

    if info.convert_leading_day_letter:
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
    if len(picture) > _MAX_CACHEABLE_PICTURE:
        return _uncached__compute_day_words_converted(picture)
    return _cached__compute_day_words_converted(picture)


@lru_cache(maxsize=256)
def _cached__compute_day_words_converted(picture: str) -> bool:
    return _uncached__compute_day_words_converted(picture)


def _uncached__compute_day_words_converted(picture: str) -> bool:
    # `parse` calls this and discards the result; it is pure, so the call is a
    # no-op, but it is left in place (memoized, so it costs one dict probe)
    # rather than removed as part of a caching change.
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


@dataclass(frozen=True, slots=True)
class _FieldSpec:
    """A parsed picture component. Frozen because `_build_regex` caches these
    and hands the same instances to every subsequent `$toMillis` call."""

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


def _build_regex(picture: str) -> tuple[re.Pattern[str], tuple[tuple[str, _FieldSpec], ...]]:
    if len(picture) > _MAX_CACHEABLE_PICTURE:
        return _uncached__build_regex(picture)
    return _cached__build_regex(picture)


@lru_cache(maxsize=256)
def _cached__build_regex(picture: str) -> tuple[re.Pattern[str], tuple[tuple[str, _FieldSpec], ...]]:
    return _uncached__build_regex(picture)


def _uncached__build_regex(picture: str) -> tuple[re.Pattern[str], tuple[tuple[str, _FieldSpec], ...]]:
    """Compiles the picture into one regex plus the group -> component map.

    Memoized: this is the single most expensive step of `$toMillis`, it is
    pure in the picture string, and both the compiled pattern and the frozen
    `_FieldSpec`s it hands out are immutable. D3132, D3133, D3135 and the
    `[x]`/`[w]` D3136 escape the cache, since `lru_cache` does not memoise
    exceptions -- which is what we want: they stay raised on every call.
    """
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
    if len(pattern) > _MAX_REGEX_CHARS:
        # The only place a picture string becomes a COMPILED REGEX, and so
        # the only place its size escapes this module's own caches:
        # `re.compile` populates the stdlib's `re._cache`, which is bounded
        # at 512 ENTRIES but not by their size. Measured, 40 pictures of
        # 200 KB left 64 MB in that cache -- ~800 MB at its entry limit --
        # even though every cache in this file correctly declined them.
        # A picture is a format specification; the longest in the entire
        # acceptance suite is 57 characters, so a pattern beyond this
        # bound cannot be a real one and is rejected rather than compiled.
        raise RuntimeEvaluationError("D3135", "$toMillis: picture string is too long")
    return re.compile(pattern, re.IGNORECASE), tuple(field_order)


def _extract_fields(m: re.Match[str], field_order: tuple[tuple[str, _FieldSpec], ...]) -> dict[str, Any]:
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
