"""Formats a timestamp (epoch millis) using an XPath/XQuery
fn:format-dateTime picture string, for $now/$fromMillis with a picture arg.

Ported from org.json_kula.jsonata_jvm.runtime.datetime.PictureFormatter.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from datetime import date as _date
from functools import lru_cache
from typing import NamedTuple

from ...errors import _RuntimeEvaluationError as RuntimeEvaluationError
from ..numeric import integer_picture as _int_picture
from . import epoch as _epoch
from . import timezones as _timezones

# A picture string (and therefore any modifier sliced out of it) can come
# from input data -- `$fromMillis(t, doc.pic)` -- so its length is
# caller-controlled. lru_cache's maxsize bounds the ENTRY COUNT, not the
# memory those entries retain: measured on an equivalent cache, 60 distinct
# 200 KB pictures held 12.3 MB, and a full 256-entry cache of them ~51 MB.
# A real picture is tens of characters, so anything longer is simply not
# cached -- identical result, slower, O(1) memory.
_MAX_CACHEABLE_PICTURE = 256

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
    # _epoch, not datetime.fromtimestamp: the latter goes through the
    # platform C library, which rejects every negative timestamp on
    # Windows -- so $fromMillis of any pre-1970 instant raised instead of
    # formatting, for every picture string.
    dt = _epoch.to_datetime(millis, offset)
    return _apply_picture(dt, picture)


# =============================================================================
# Picture-string application
# =============================================================================


# A component handler: (timestamp, modifier) -> rendered text.
_Handler = Callable[[datetime, str], str]

# (leading literal, ((handler, modifier, trailing literal), ...)).
_Compiled = tuple[str, tuple[tuple[_Handler, str, str], ...]]


def _apply_picture(dt: datetime, picture: str) -> str:
    head, items = _compile_picture(picture)
    if not items:
        return head
    out: list[str] = [head]
    append = out.append
    for handler, mod, literal in items:
        append(handler(dt, mod))
        append(literal)
    return "".join(out)


def _compile_picture(picture: str) -> _Compiled:
    if len(picture) > _MAX_CACHEABLE_PICTURE:
        return _uncached__compile_picture(picture)
    return _cached__compile_picture(picture)


@lru_cache(maxsize=256)
def _cached__compile_picture(picture: str) -> _Compiled:
    return _uncached__compile_picture(picture)


def _uncached__compile_picture(picture: str) -> _Compiled:
    """Splits the picture into its literal runs and its variable components,
    resolving each component to the handler that renders it.

    Memoized, because the picture is a compile-time literal at essentially
    every `$fromMillis` call site -- jsonata2js re-scans it on every call and
    even says so, `datetime.js:884`: `// TODO can cache this against the
    picture`. Nothing in the result depends on the timestamp, the timezone or
    any other per-evaluation state, and the result is a tuple of tuples of
    strings and functions, so a cached entry is immutable and safe to share
    across evaluations and threads.

    D3135 escapes the cache, since `lru_cache` does not memoise exceptions --
    which is what we want: it stays raised on every call. An unknown component
    is deliberately NOT raised here but resolved to a raising handler, so it
    still fires in picture order: `[YN]-[q]` must report `[YN]`'s D3133, not
    the unknown `[q]`.
    """
    check_brackets(picture)
    head = ""
    items: list[tuple[_Handler, str, str]] = []
    buf: list[str] = []
    i = 0
    n = len(picture)
    while i < n:
        c = picture[i]
        if c == "[" and i + 1 < n and picture[i + 1] == "[":
            buf.append("[")
            i += 2
        elif c == "[":
            j = picture.find("]", i + 1)
            if j < 0:
                raise _unclosed()
            spec = _WS_RE.sub("", picture[i + 1 : j])
            i = j + 1
            # `[]` and `[ ]` format as the empty string, so they just merge
            # into the surrounding literal run.
            if not spec:
                continue
            literal = "".join(buf)
            buf.clear()
            if items:
                # The literal that preceded this component terminates the
                # previous one; the very first run is the head.
                handler, mod, _ = items[-1]
                items[-1] = (handler, mod, literal)
            else:
                head = literal
            resolved = _COMPONENTS.get(spec[0])
            items.append((resolved if resolved is not None else _unknown_component(spec), spec[1:], ""))
        elif c == "]" and i + 1 < n and picture[i + 1] == "]":
            buf.append("]")
            i += 2
        else:
            buf.append(c)
            i += 1
    tail = "".join(buf)
    if items:
        handler, mod, _ = items[-1]
        items[-1] = (handler, mod, tail)
    else:
        head = tail
    return head, tuple(items)


def _unknown_component(spec: str) -> _Handler:
    def raise_unknown(dt: datetime, mod: str) -> str:
        raise RuntimeEvaluationError(None, f"Unknown picture-string component: [{spec}]")

    return raise_unknown


# =============================================================================
# Component formatting
# =============================================================================


def _present(value: int, mod: str) -> str | None:
    """Renders `value` when `mod` selects a non-decimal numbering, else
    None so the caller falls through to its decimal path.

    §9.8.4.3 of XPath F&O routes every integer-valued date/time component
    through the *same* integer formatter `$formatInteger` uses, which is
    what the reference does (jsonata2js `datetime.js:906`,
    `_formatInteger(componentValue, markerSpec.integerFormat)`). Python
    instead hand-wrote a partial modifier set per component, so `[Ya]`,
    `[YA]`, `[YWw]`, `[DW]`, `[Mw]` and friends silently fell back to
    plain decimal or raised.

    Sharing `integer_picture.format` also means `$formatInteger` and
    these components can never drift apart again. The one translation
    needed is the ordinal marker: a date/time modifier spells it as a
    trailing "o" (`[D1o]`, `[Dwo]`) where an integer picture spells it
    as a `;o` suffix.

    Which integer picture a modifier maps to is picture-only, so the
    translation is memoized by `_present_picture` and only the final
    `integer_picture.format` call is left per timestamp.
    """
    pic = _present_picture(mod)
    if pic is None:
        return None
    return _int_picture.format(value, pic)


def _present_picture(mod: str) -> str | None:
    if len(mod) > _MAX_CACHEABLE_PICTURE:
        return _uncached__present_picture(mod)
    return _cached__present_picture(mod)


@lru_cache(maxsize=512)
def _cached__present_picture(mod: str) -> str | None:
    return _uncached__present_picture(mod)


def _uncached__present_picture(mod: str) -> str | None:
    """The `$formatInteger` picture a date/time modifier selects, or None when
    it selects plain decimal and the caller must fall through."""
    if not mod:
        return None
    core = mod.split(",", 1)[0]
    ordinal = len(core) > 1 and core.endswith("o")
    if ordinal:
        core = core[:-1]
    if not core or core[0] not in "aAiIwW":
        return None
    return f"{core};o" if ordinal else core


# One handler per component letter, resolved once by `_compile_picture`. The
# reference walks a `switch` on every component of every call
# (datetime.js:892-1000); a dict probe at compile time replaces the whole
# chain of up to eighteen string comparisons per component per call.


def _format_iso_year(dt: datetime, mod: str) -> str:
    iso_year, _iso_week, _iso_wd = dt.isocalendar()
    return _present(iso_year, mod) or _format_int(iso_year, mod, 4)


def _format_iso_week(dt: datetime, mod: str) -> str:
    _iso_year, iso_week, _iso_wd = dt.isocalendar()
    return _present(iso_week, mod) or _format_int(iso_week, mod, 2)


def _format_hour24(dt: datetime, mod: str) -> str:
    return _present(dt.hour, mod) or _format_int(dt.hour, mod, 2)


def _format_hour12(dt: datetime, mod: str) -> str:
    h12 = dt.hour % 12 or 12
    return _present(h12, mod) or _format_int(h12, mod, 2)


def _format_calendar(dt: datetime, mod: str) -> str:
    return "ISO"


def _format_minute(dt: datetime, mod: str) -> str:
    return _present(dt.minute, mod) or _format_int(dt.minute, mod if mod else "01", 2)


def _format_second(dt: datetime, mod: str) -> str:
    return _present(dt.second, mod) or _format_int(dt.second, mod if mod else "01", 2)


def _format_fraction(dt: datetime, mod: str) -> str:
    ms = dt.microsecond // 1000
    return _present(ms, mod) or _format_millis(ms, mod)


def _format_offset_z_component(dt: datetime, mod: str) -> str:
    return _format_offset_z(dt.utcoffset(), mod)


def _format_offset_name_component(dt: datetime, mod: str) -> str:
    return _format_offset_name(dt.utcoffset(), mod)


def _format_year(dt: datetime, mod: str) -> str:
    if mod in ("N", "n"):
        raise RuntimeEvaluationError("D3133", "Year name component is not supported")
    presented = _present(dt.year, mod)
    if presented is not None:
        return presented
    if mod and "o" in mod:
        # An ordinal needs a digit or alphabetic picture to attach to;
        # a bare `[Yo]` is rejected by the reference with D3130.
        digits = mod.replace("o", "")
        if not digits:
            raise RuntimeEvaluationError("D3130", "$formatInteger: invalid picture string")
        return _int_picture.format(dt.year, f"{digits};o")
    return _format_int(dt.year, mod, 4)


def _format_month(dt: datetime, mod: str) -> str:
    if mod and mod[0] in ("n", "N"):
        return _format_month_name(dt, mod)
    presented = _present(dt.month, mod)
    if presented is not None:
        return presented
    return _format_int(dt.month, mod, 2)


def _format_day_of_month(dt: datetime, mod: str) -> str:
    presented = _present(dt.day, mod)
    if presented is not None:
        return presented
    if mod and "o" in mod:
        return _format_ordinal_suffix(dt.day)
    return _format_int(dt.day, mod, 2)


def _day_of_year(dt: datetime) -> int:
    return (dt.date() - _date(dt.year, 1, 1)).days + 1


def _format_day_of_year(dt: datetime, mod: str) -> str:
    doy = _day_of_year(dt)
    presented = _present(doy, mod)
    if presented is not None:
        return presented
    return _format_int(doy, mod, 3)


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

    # A names presentation ('N') carries no digit count, so there is no
    # width for the offset: the reference rejects it (jsonata2js
    # `datetime.js:926`, D3134) rather than silently formatting.
    if mod and not any(c.isdigit() for c in mod) and any(c.isalpha() for c in mod if c != "t"):
        raise RuntimeEvaluationError("D3134", f"Invalid timezone picture modifier: {mod}")
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


def _format_offset_name(delta: timedelta | None, mod: str) -> str:
    """`[z]` -- identical to `[Z]` except for the "GMT" prefix, and with
    no collapse to "Z"/"GMT" for a zero offset: the reference renders
    +00:00 as "GMT+00:00" (jsonata2js `datetime.js:932-937` only applies
    the 'Z' collapse when presentation2 is 't', and the GMT prefix is
    added around the same integer-formatted offset `[Z]` uses).

    A bare `[z]` therefore behaves as `[z01:01]`, and `[z0]` -- one
    mandatory digit -- prints the hours alone, appending ":mm" only when
    the minutes are non-zero ("GMT+0", "GMT+5:30", "GMT-8").
    """
    total_mins = (int(delta.total_seconds()) // 60) if delta is not None else 0
    h = abs(total_mins) // 60
    m = abs(total_mins) % 60
    sign = "+" if total_mins >= 0 else "-"

    if mod and not any(c.isdigit() for c in mod):
        raise RuntimeEvaluationError("D3134", f"Invalid timezone picture modifier: {mod}")
    digits = [c for c in mod if c.isdigit()]
    if not mod or ":" in mod:
        body = f"{h:02d}:{m:02d}"
    elif len(digits) <= 2:
        body = f"{h}" if len(digits) == 1 else f"{h:02d}"
        if m != 0:
            body += f":{m:02d}"
    elif len(digits) <= 4:
        body = f"{h:02d}{m:02d}"
    else:
        raise RuntimeEvaluationError("D3134", "timezone picture string too long")
    return f"GMT{sign}{body}"


# =============================================================================
# Week-of-month helpers
# =============================================================================


def _monday_on_or_before(d: _date) -> _date:
    while d.isoweekday() != 1:
        d = d - timedelta(days=1)
    return d


def _format_week_of_month(dt: datetime, mod: str) -> str:
    """Weeks are counted from the start of the first week of the month --
    ported from jsonata2js `datetime.js:765-783`. The rule this replaced
    was day-of-month/7 with ad-hoc corrections, which put 1970-01-01,
    2024-02-01 and 2025-01-01 in week 5 instead of week 1.
    """
    date = dt.date()
    week = _delta_weeks(_start_of_first_week(date.year, date.month), date)
    if week > 4:
        following = _shifted_first_week(date.year, date.month, 1)
        if following is not None and date >= following:
            week = 1
    elif week < 1:
        previous = _shifted_first_week(date.year, date.month, -1)
        if previous is not None:
            week = _delta_weeks(previous, date)
    return _format_int(week, mod, 1)


def _shifted_first_week(year: int, month: int, delta: int) -> _date | None:
    """First week of the adjacent month, or None when that month falls
    outside date's year 1..9999 range (JavaScript's Date spans further,
    so the reference never has to consider it)."""
    m, y = _shift_month(year, month, delta)
    try:
        return _start_of_first_week(y, m)
    except ValueError:
        return None


def _start_of_first_week(year: int, month: int) -> _date:
    """Start of the month's first week, per ISO 8601 as extended to
    months by XPath F&O: the week (Monday-based) containing the month's
    first Thursday. So when the 1st is a Friday, Saturday or Sunday the
    first week starts on the FOLLOWING Monday, not the preceding one --
    the adjustment this function was missing.
    """
    first = _date(year, month, 1)
    dow = first.isoweekday()  # Mon=1 .. Sun=7
    if dow > 4:
        return first + timedelta(days=8 - dow)
    return first - timedelta(days=dow - 1)


def _delta_weeks(start: _date, end: _date) -> int:
    return (end - start).days // 7 + 1


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


_COMPONENTS: dict[str, _Handler] = {
    "Y": _format_year,
    "X": _format_iso_year,
    "W": _format_iso_week,
    "w": _format_week_of_month,
    "x": _format_week_of_month_context,
    "M": _format_month,
    "D": _format_day_of_month,
    "d": _format_day_of_year,
    "F": _format_day_name,
    "H": _format_hour24,
    "h": _format_hour12,
    "C": _format_calendar,
    "E": _format_calendar,
    "m": _format_minute,
    "s": _format_second,
    "f": _format_fraction,
    "P": _format_am_pm,
    "Z": _format_offset_z_component,
    "z": _format_offset_name_component,
}


# =============================================================================
# Number formatting helpers
# =============================================================================


class _IntMod(NamedTuple):
    """`_format_int`'s width modifier, parsed. Depends only on the picture."""

    plain: bool  # the modifier asks for a bare str(value)
    min_width: int  # 0 -> no zero padding
    truncate_to: int  # -1 -> never truncate
    star_group: bool  # `,*` -- insert a "," every three digits


def _int_mod(mod: str, default_width: int) -> _IntMod:
    if len(mod) > _MAX_CACHEABLE_PICTURE:
        return _uncached__int_mod(mod, default_width)
    return _cached__int_mod(mod, default_width)


@lru_cache(maxsize=512)
def _cached__int_mod(mod: str, default_width: int) -> _IntMod:
    return _uncached__int_mod(mod, default_width)


def _uncached__int_mod(mod: str, default_width: int) -> _IntMod:
    if not mod or mod == "1" or (mod.startswith("#") and "," not in mod):
        return _IntMod(True, 0, -1, False)

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

    # A leading "9" means "no minimum width", which is exactly what
    # `_zfill_signed(value, 0)` produces for either sign.
    if mod.startswith("9"):
        min_width = 0

    truncate_to = -1
    if use_thousands_sep and max_width is not None:
        parts = mod.split(",")
        is_min_max = len(parts) > 1 and "-" in parts[1]
        if is_min_max or not has_zeros_in_min_part:
            truncate_to = max_width

    return _IntMod(False, min_width, truncate_to, use_thousands_sep and "*" in mod)


def _format_int(value: int, mod: str, default_width: int) -> str:
    spec = _int_mod(mod, default_width)
    if spec.plain:
        return str(value)

    formatted = _zfill_signed(value, spec.min_width)

    truncate_to = spec.truncate_to
    if truncate_to >= 0 and len(formatted) > truncate_to:
        formatted = formatted[-truncate_to:] if truncate_to > 0 else ""

    if spec.star_group:
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
    """`[f]` -- the reference does NOT treat this as a decimal fraction.
    It formats the raw millisecond value through the ordinary
    integer-picture path (jsonata2js `datetime.js:908-910`, whose own
    comment flags that F&O §9.8.4.5 is unimplemented), so `[f0001]` on
    1 ms is "0001", not the "0010" a scaled fraction would give.

    A `,min-max` width modifier only ever pads here: `[f1,3-3]` on 1 ms
    is "001", while `[f1,1-1]` on 123 ms stays "123" rather than being
    truncated to one digit.
    """
    return str(millis).zfill(_millis_width(mod))


def _millis_width(mod: str) -> int:
    if len(mod) > _MAX_CACHEABLE_PICTURE:
        return _uncached__millis_width(mod)
    return _cached__millis_width(mod)


@lru_cache(maxsize=512)
def _cached__millis_width(mod: str) -> int:
    return _uncached__millis_width(mod)


def _uncached__millis_width(mod: str) -> int:
    if not mod:
        return 0
    digits_part, _, width_part = mod.partition(",")
    width = sum(1 for c in digits_part if c.isdigit() and c != "1") or 0
    if "0" in digits_part:
        width = sum(1 for c in digits_part if c.isdigit())
    if width_part:
        lo = re.match(r"(\d+)", width_part)
        if lo:
            width = max(width, int(lo.group(1)))
    return width


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
