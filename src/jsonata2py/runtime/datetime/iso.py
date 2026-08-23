"""ISO 8601 conversion for $now/$millis/$fromMillis/$toMillis.

Ported from org.json_kula.jsonata_jvm.runtime.datetime.IsoConverter.

millis_to_picture / picture_to_millis delegate to the XPath/XQuery
picture-string engines in picture_formatter.py / picture_parser.py.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime
from datetime import timezone as _tz

from ...errors import _RuntimeEvaluationError as RuntimeEvaluationError
from . import timezones as _timezones

_YEAR_ONLY = re.compile(r"\d{4}")
_YEAR_MONTH = re.compile(r"\d{4}-\d{2}")


def millis_to_iso(millis: int, timezone: str | None = "UTC") -> str:
    offset = _timezones.parse_zone_offset(timezone)
    dt = datetime.fromtimestamp(millis / 1000.0, tz=UTC).astimezone(offset)
    total_seconds = offset.utcoffset(None).total_seconds() if offset.utcoffset(None) else 0
    tz_suffix = "Z" if total_seconds == 0 else _offset_id(offset)
    ms = millis % 1000
    if millis < 0 and ms != 0:
        ms += 1000
    return (
        f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}T"
        f"{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}.{ms:03d}{tz_suffix}"
    )


def _offset_id(offset: _tz) -> str:
    delta = offset.utcoffset(None)
    total_minutes = int(delta.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    return f"{sign}{total_minutes // 60:02d}:{total_minutes % 60:02d}"


def iso_to_millis(timestamp: str) -> int:
    # Fast path: standard ISO 8601 instant.
    parsed = _try_parse_instant(timestamp)
    if parsed is not None:
        return parsed

    normalised = _timezones.normalize_offset_in_timestamp(timestamp)
    if normalised != timestamp:
        parsed = _try_parse_instant(normalised)
        if parsed is not None:
            return parsed

    try:
        if _YEAR_ONLY.fullmatch(timestamp):
            dt = datetime(int(timestamp), 1, 1, tzinfo=UTC)
            return int(dt.timestamp() * 1000)
        if _YEAR_MONTH.fullmatch(timestamp):
            year, month = timestamp.split("-")
            dt = datetime(int(year), int(month), 1, tzinfo=UTC)
            return int(dt.timestamp() * 1000)
        # Java's final fallback is LocalDate.parse -- a *date-only* parse
        # (yyyy-MM-dd) that rejects anything else, notably a trailing time
        # component with a space instead of 'T' (not valid ISO 8601).
        # date.fromisoformat is the equivalent strict date-only parse;
        # datetime.fromisoformat here would wrongly accept that.
        d = date.fromisoformat(timestamp)
        dt = datetime(d.year, d.month, d.day, tzinfo=UTC)
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        raise RuntimeEvaluationError("D3110", f"$toMillis: invalid ISO 8601 timestamp: {timestamp}") from None


def _try_parse_instant(timestamp: str) -> int | None:
    try:
        s = timestamp.replace("Z", "+00:00") if timestamp.endswith(("Z", "z")) else timestamp
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            return None
        return int(dt.timestamp() * 1000)
    except ValueError:
        return None


def millis_to_picture(millis: int, picture: str, timezone: str | None) -> str:
    from . import picture_formatter

    return picture_formatter.format(millis, picture, timezone)


def picture_to_millis(timestamp: str, picture: str) -> int | None:
    from . import picture_parser

    return picture_parser.parse(timestamp, picture)
