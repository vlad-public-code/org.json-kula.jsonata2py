"""Formats a timestamp (epoch millis) using an XPath/XQuery
fn:format-dateTime picture string, for $now/$fromMillis with a picture arg.

Ported from org.json_kula.jsonata_jvm.runtime.datetime.PictureFormatter.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime, timedelta
from datetime import date as _date

from ...errors import _RuntimeEvaluationError as RuntimeEvaluationError
from . import roman as _roman
from . import timezones as _timezones
from . import word_numbers as _words

_WS_RE = re.compile(r"\s+")

_DAY_NAMES_FULL = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
_DAY_NAMES_SHORT = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_DAY_NAMES_NARROW = ["M", "T", "W", "T", "F", "S", "S"]

_MONTH_NAMES_FULL = [
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
]
_MONTH_NAMES_SHORT = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
_MONTH_NAMES_NARROW = ["J", "F", "M", "A", "M", "J", "J", "A", "S", "O", "N", "D"]


def format(millis: int, picture: str, timezone: str | None) -> str:
    offset = UTC if not timezone else _timezones.parse_zone_offset(timezone)
    dt = datetime.fromtimestamp(millis / 1000.0, tz=UTC).astimezone(offset)
    return _apply_picture(dt, picture)


# =============================================================================
# Picture-string application
# =============================================================================


def _apply_picture(dt: datetime, picture: str) -> str:
    check_brackets(picture)
    out: list[str] = []
    i = 0
    n = len(picture)
    while i < n:
        c = picture[i]
        if c == "[" and i + 1 < n and picture[i + 1] == "[":
            out.append("[")
            i += 2
        elif c == "[":
            j = picture.find("]", i + 1)
            if j < 0:
                raise _unclosed()
            out.append(_format_component(dt, picture[i + 1 : j]))
            i = j + 1
        elif c == "]" and i + 1 < n and picture[i + 1] == "]":
            out.append("]")
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


# =============================================================================
# Component formatting
# =============================================================================


def _format_component(dt: datetime, spec: str) -> str:
    if not spec:
        return ""
    spec = _WS_RE.sub("", spec)
    d = spec[0]
    mod = spec[1:]

    if d == "Y":
        return _format_year(dt, mod)
    if d == "X":
        iso_year, _iso_week, _iso_wd = dt.isocalendar()
        return _format_int(iso_year, mod, 4)
    if d == "W":
        _iso_year, iso_week, _iso_wd = dt.isocalendar()
        return _format_int(iso_week, mod, 2)
    if d == "w":
        return _format_week_of_month(dt, mod)
    if d == "x":
        return _format_week_of_month_context(dt, mod)
    if d == "M":
        return _format_month(dt, mod)
    if d == "D":
        return _format_day_of_month(dt, mod)
    if d == "d":
        return _format_day_of_year(dt, mod)
    if d == "F":
        return _format_day_name(dt, mod)
    if d == "H":
        return _format_int(dt.hour, mod, 2)
    if d == "h":
        h = dt.hour % 12
        return _format_int(12 if h == 0 else h, mod, 2)
    if d in ("C", "E"):
        return "ISO"
    if d == "m":
        return _format_int(dt.minute, mod if mod else "01", 2)
    if d == "s":
        return _format_int(dt.second, mod if mod else "01", 2)
    if d == "f":
        return _format_millis(dt.microsecond // 1000, mod)
    if d == "P":
        return _format_am_pm(dt, mod)
    if d == "Z":
        return _format_offset_z(dt.utcoffset(), mod)
    if d == "z":
        return _format_offset_name(dt.utcoffset())
    raise RuntimeEvaluationError(None, f"Unknown picture-string component: [{spec}]")


def _format_year(dt: datetime, mod: str) -> str:
    if mod:
        if mod in ("N", "n"):
            raise RuntimeEvaluationError("D3133", "Year name component is not supported")
        if mod == "I":
            return _roman.to_roman(dt.year)
        if mod == "i":
            return _roman.to_roman(dt.year).lower()
        if mod in ("w", "W"):
            return _words.to_cardinal(dt.year)
    return _format_int(dt.year, mod, 4)


def _format_month(dt: datetime, mod: str) -> str:
    if mod and mod[0].isalpha():
        if mod in ("a", "A"):
            return _to_alphabetic(dt.month, mod == "A")
        if mod[0] in ("n", "N"):
            return _format_month_name(dt, mod)
        if mod == "i":
            return _roman.to_roman(dt.month).lower()
        if mod == "I":
            return _roman.to_roman(dt.month)
    return _format_int(dt.month, mod, 2)


def _format_day_of_month(dt: datetime, mod: str) -> str:
    if mod:
        if "w" in mod:
            return _words.to_ordinal(dt.day)
        if "o" in mod:
            return _format_ordinal_suffix(dt.day)
        if mod in ("a", "A"):
            return _to_alphabetic(dt.day, mod == "A")
    return _format_int(dt.day, mod, 2)


def _day_of_year(dt: datetime) -> int:
    return (dt.date() - _date(dt.year, 1, 1)).days + 1


def _format_day_of_year(dt: datetime, mod: str) -> str:
    if mod and "w" in mod:
        return _words.to_ordinal(_day_of_year(dt))
    return _format_int(_day_of_year(dt), mod, 3)


def _format_day_name(dt: datetime, mod: str) -> str:
    idx = dt.isoweekday() - 1  # 0 = Monday
    if "0" in mod or "1" in mod:
        return str(dt.isoweekday())

    if "," in mod:
        return _abbreviate_name(_DAY_NAMES_SHORT[idx], mod)

    name = _DAY_NAMES_FULL[idx]
    if not mod or mod == "n":
        return name.lower()
    if mod == "N":
        return name.upper()
    if mod.startswith("N") and "n" in mod:
        return _title_case(name)
    if mod in ("a", "A"):
        abbr = _DAY_NAMES_NARROW[idx]
        return abbr.lower() if mod == "a" else abbr
    return _title_case(name)


def _format_month_name(dt: datetime, mod: str) -> str:
    idx = dt.month - 1
    if not mod or re.match(r"^\d", mod):
        return str(dt.month)
    if "," in mod:
        return _abbreviate_name(_MONTH_NAMES_SHORT[idx], mod)
    name = _MONTH_NAMES_FULL[idx]
    if mod == "n" or mod.startswith("n"):
        return name.lower()
    if mod == "N":
        return name.upper()
    if mod.startswith("N") and "n" in mod:
        return _title_case(name)
    if len(mod) == 1 and mod.isalpha():
        return _MONTH_NAMES_NARROW[idx]
    return _MONTH_NAMES_SHORT[idx]


def _format_am_pm(dt: datetime, mod: str) -> str:
    afternoon = dt.hour >= 12
    if mod == "N":
        return "PM" if afternoon else "AM"
    return "pm" if afternoon else "am"


def _format_offset_z(delta: timedelta | None, mod: str) -> str:
    total_secs = int(delta.total_seconds()) if delta is not None else 0
    total_mins = total_secs // 60
    h = abs(total_mins) // 60
    m = abs(total_mins) % 60

    hour_width = 2
    minute_width = 2
    use_colon = True
    short_format = "t" in mod

    if mod:
        if ":" in mod:
            use_colon = True
            parts = mod.split(":")
            hour_width = 2 if not parts[0] else len(parts[0])
            min_part = parts[1].replace("t", "") if len(parts) > 1 else ""
            minute_width = 2 if not min_part else len(min_part)
        elif mod == "0":
            if m == 0:
                minute_width = 0
                hour_width = 1
                use_colon = False
            else:
                use_colon = True
                hour_width = 1
        else:
            digits = "".join(c for c in mod if c.isdigit())
            if len(digits) > 4:
                raise RuntimeEvaluationError("D3134", "timezone picture string too long")
            use_colon = False
            hour_width = 2 if len(digits) >= 2 else hour_width
            if len(digits) >= 4:
                minute_width = 2
            elif short_format:
                minute_width = 0

    if total_secs == 0 and short_format:
        return "Z"

    hour_str = str(h) if hour_width == 1 else str(h).zfill(hour_width)
    sign = "+" if total_mins >= 0 else "-"

    if minute_width == 0:
        return sign + hour_str
    min_str = str(m).zfill(minute_width)
    return f"{sign}{hour_str}:{min_str}" if use_colon else f"{sign}{hour_str}{min_str}"


def _format_offset_name(delta: timedelta | None) -> str:
    total_secs = int(delta.total_seconds()) if delta is not None else 0
    if total_secs == 0:
        return "GMT"
    total_mins = total_secs // 60
    h = abs(total_mins) // 60
    m = abs(total_mins) % 60
    sign = "+" if total_mins >= 0 else "-"
    return f"GMT{sign}{h:02d}:{m:02d}"


# =============================================================================
# Week-of-month helpers
# =============================================================================


def _monday_on_or_before(d: _date) -> _date:
    while d.isoweekday() != 1:
        d = d - timedelta(days=1)
    return d


def _format_week_of_month(dt: datetime, mod: str) -> str:
    date = dt.date()
    monday = _monday_on_or_before(date)
    in_current = sum(1 for i in range(7) if (monday + timedelta(days=i)).month == date.month)
    if in_current <= 3 and date.day >= 28:
        return "1"
    if monday.month != date.month and date.day <= 4:
        return "5"
    return _format_int(math.ceil(date.day / 7), mod, 1)


def _format_week_of_month_context(dt: datetime, mod: str) -> str:
    date = dt.date()
    monday = _monday_on_or_before(date)
    first_of_month = date.replace(day=1)
    prev = curr = nxt = 0
    for i in range(7):
        day = monday + timedelta(days=i)
        if day < first_of_month:
            prev += 1
        elif day.month == date.month:
            curr += 1
        else:
            nxt += 1
    if prev > curr and prev > nxt:
        ctx_month, ctx_year = _shift_month(date.year, date.month, -1)
    elif nxt > curr and nxt > prev:
        ctx_month, ctx_year = _shift_month(date.year, date.month, 1)
    else:
        ctx_month, ctx_year = date.month, date.year
    idx = ctx_month - 1
    if mod and "N" in mod:
        name = _MONTH_NAMES_FULL[idx]
        return name.lower() if mod.startswith("n") else name
    return _format_month_name(dt.replace(year=ctx_year, month=ctx_month, day=1), mod)


def _shift_month(year: int, month: int, delta: int) -> tuple[int, int]:
    total = (year * 12 + (month - 1)) + delta
    return total % 12 + 1, total // 12


# =============================================================================
# Number formatting helpers
# =============================================================================


def _format_int(value: int, mod: str, default_width: int) -> str:
    if not mod or mod == "1" or (mod.startswith("#") and "," not in mod):
        return str(value)

    use_thousands_sep = "," in mod
    min_width = default_width
    max_width = None
    has_zeros_in_min_part = False

    if use_thousands_sep:
        parts = mod.split(",")
        if parts and parts[0]:
            min_part = parts[0]
            for i, ch in enumerate(min_part):
                if ch == "0" and (i == 0 or min_part[i - 1] != "#"):
                    has_zeros_in_min_part = True
                    break
            if has_zeros_in_min_part:
                w = sum(1 for c in min_part if c.isdigit())
                if w > 0:
                    min_width = w
        if len(parts) > 1 and parts[1]:
            digits = re.sub(r"[^0-9].*", "", parts[1])
            if digits:
                max_width = int(digits)
    else:
        w = sum(1 for c in mod if c.isdigit())
        if w > 0:
            min_width = w
        has_zeros_in_min_part = "0" in mod

    no_min_width = mod.startswith("9")
    formatted = str(value) if no_min_width else _zfill_signed(value, min_width)

    if use_thousands_sep and max_width is not None and len(formatted) > max_width:
        parts = mod.split(",")
        is_min_max = len(parts) > 1 and "-" in parts[1]
        no_zeros_in_min = not has_zeros_in_min_part
        if is_min_max or no_zeros_in_min:
            formatted = formatted[-max_width:] if max_width > 0 else ""

    if use_thousands_sep and "*" in mod:
        digits_rev = formatted[::-1]
        grouped = []
        for i, c in enumerate(digits_rev):
            if i > 0 and i % 3 == 0:
                grouped.append(",")
            grouped.append(c)
        return "".join(reversed(grouped))
    return formatted


def _zfill_signed(value: int, width: int) -> str:
    if value < 0:
        return "-" + str(-value).zfill(max(0, width - 1))
    return str(value).zfill(width)


def _format_millis(millis: int, mod: str) -> str:
    width = sum(1 for c in mod if c.isdigit())
    if width == 0:
        width = 3
    scaled = millis // (10 ** (3 - width)) if width <= 3 else millis * (10 ** (width - 3))
    return str(scaled).zfill(width)


def _format_ordinal_suffix(n: int) -> str:
    if n in (1, 21, 31):
        suffix = "st"
    elif n in (2, 22):
        suffix = "nd"
    elif n in (3, 23):
        suffix = "rd"
    else:
        suffix = "th"
    return f"{n}{suffix}"


def _to_alphabetic(n: int, uppercase: bool) -> str:
    if n <= 0:
        return str(n)
    out: list[str] = []
    while n > 0:
        n -= 1
        out.append(chr(ord("a") + (n % 26)))
        n //= 26
    s = "".join(reversed(out))
    return s.upper() if uppercase else s


def _title_case(s: str) -> str:
    if not s:
        return s
    return s[0].upper() + s[1:].lower()


def _abbreviate_name(name: str, mod: str) -> str:
    parts = mod.split(",")
    if len(parts) > 1 and "-" in parts[1]:
        max_len_str = parts[1].split("-")[0]
        try:
            max_len = int(max_len_str)
        except ValueError:
            return name
        if parts[0] and parts[0][0] == "N":
            cased = _title_case(name)
        elif "n" in parts[0]:
            cased = name.lower()
        else:
            cased = name
        return cased[: min(max_len, len(cased))]
    return name


# =============================================================================
# Bracket validation
# =============================================================================


def check_brackets(picture: str) -> None:
    count = 0
    n = len(picture)
    k = 0
    while k < n:
        c = picture[k]
        if c == "[" and k + 1 < n and picture[k + 1] == "[":
            k += 2
            continue
        if c == "]" and k + 1 < n and picture[k + 1] == "]":
            k += 2
            continue
        if c == "[":
            count += 1
        elif c == "]":
            count -= 1
        k += 1
    if count != 0:
        raise _unclosed()


def _unclosed() -> RuntimeEvaluationError:
    return RuntimeEvaluationError("D3135", "Unclosed '[' in picture string")
