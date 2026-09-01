"""ISO 8601 conversion for $now/$millis/$fromMillis/$toMillis.

Ported from org.json_kula.jsonata_jvm.runtime.datetime.IsoConverter.

millis_to_picture / picture_to_millis delegate to the XPath/XQuery
picture-string engines in picture_formatter.py / picture_parser.py.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from datetime import timezone as _tz

from ...errors import _RuntimeEvaluationError as RuntimeEvaluationError
from . import epoch as _epoch
from . import timezones as _timezones

_YEAR_ONLY = re.compile(r"\d{4}")
_YEAR_MONTH = re.compile(r"\d{4}-\d{2}")
_CALENDAR_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")


def millis_to_iso(millis: int, timezone: str | None = "UTC") -> str:
    offset = _timezones.parse_zone_offset(timezone)
    dt = _epoch.to_datetime(millis, offset)
    total_seconds = offset.utcoffset(None).total_seconds() if offset.utcoffset(None) else 0
    tz_suffix = "Z" if total_seconds == 0 else _offset_id(offset)
    # Python's % already returns a non-negative remainder for a positive
    # modulus, so the Java-style negative correction this used to carry
    # turned -1 ms into 1999 and printed ".1999" instead of ".999".
    ms = millis % 1000
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
            return _epoch.to_millis(datetime(int(timestamp), 1, 1, tzinfo=UTC))
        if _YEAR_MONTH.fullmatch(timestamp):
            y_text, m_text = timestamp.split("-")
            return _epoch.to_millis(datetime(int(y_text), int(m_text), 1, tzinfo=UTC))
        # Java's final fallback is LocalDate.parse -- a *date-only* parse
        # (yyyy-MM-dd) that rejects anything else, notably a trailing time
        # component with a space instead of 'T' (not valid ISO 8601).
        #
        # Not date.fromisoformat: on 3.11+ that also accepts ISO week
        # dates ("2024-W01-1") and ordinal dates, which the reference
        # rejects with D3110. An explicit yyyy-mm-dd match keeps the
        # accepted set identical to the reference's.
        m = _CALENDAR_DATE.fullmatch(timestamp)
        if m is None:
            raise ValueError(timestamp)
        year, month, day = (int(g) for g in m.groups())
        if not (1 <= month <= 12) or not (1 <= day <= 31):
            # The reference produces a NaN/invalid-Date artifact here
            # ($type reports "object" for $toMillis("2023-13-01")), which
            # is not a value worth reproducing; D3110 is what an invalid
            # timestamp means everywhere else in this function.
            raise ValueError(timestamp)
        # Within that range the reference does NOT validate the day
        # against the month's length -- it constructs and lets the
        # surplus roll over, so "2023-02-29" is 2023-03-01 and
        # "2023-04-31" is 2023-05-01, not errors.
        return _epoch.to_millis(datetime(year, month, 1, tzinfo=UTC)) + (day - 1) * 86_400_000
    except (ValueError, TypeError, OverflowError):
        raise RuntimeEvaluationError("D3110", f"$toMillis: invalid ISO 8601 timestamp: {timestamp}") from None


def _try_parse_instant(timestamp: str) -> int | None:
    try:
        s = timestamp.replace("Z", "+00:00") if timestamp.endswith(("Z", "z")) else timestamp
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            return None
        return _epoch.to_millis(dt)
    except ValueError:
        return None


def millis_to_picture(millis: int, picture: str, timezone: str | None) -> str:
    from . import picture_formatter

    return picture_formatter.format(millis, picture, timezone)


def picture_to_millis(timestamp: str, picture: str) -> int | None:
    from . import picture_parser

    return picture_parser.parse(timestamp, picture)
