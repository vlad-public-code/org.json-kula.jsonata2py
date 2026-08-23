"""Timezone offset parsing for JSONata date/time functions.

Ported from org.json_kula.jsonata_jvm.runtime.datetime.TimezoneUtils.

Timezone offsets accepted: Z, UTC, empty (all UTC); +-HHMM; +-HH:MM;
GMT+-HH:MM / GMT+-HH. All are offset arithmetic, not zone-database
lookups (see docs/porting-design-spec.md D7.4).
"""

from __future__ import annotations

import re
from datetime import UTC, timedelta, timezone

from ...errors import _RuntimeEvaluationError as RuntimeEvaluationError

_GMT_OFFSET = re.compile(r"GMT([+-])(\d{1,2})(?::(\d{2}))?")


def _invalid(tz: str) -> RuntimeEvaluationError:
    return RuntimeEvaluationError("D3110", f"Invalid timezone: {tz}")


def parse_zone_offset(tz: str | None) -> timezone:
    """Parses a timezone string into a datetime.timezone offset."""
    if tz is None or tz == "" or tz in ("Z", "UTC"):
        return UTC
    if tz in ("0000", "+0000", "-0000"):
        return UTC

    if tz.startswith("GMT"):
        if len(tz) == 3:
            return UTC
        m = _GMT_OFFSET.match(tz)
        if not m or m.end() != len(tz):
            raise _invalid(tz)
        h = int(m.group(2))
        minute = int(m.group(3)) if m.group(3) is not None else 0
        secs = (h * 60 + minute) * 60
        return timezone(timedelta(seconds=-secs if m.group(1) == "-" else secs))

    sign = tz[0]
    if sign not in ("+", "-"):
        raise _invalid(tz)
    try:
        if ":" in tz:
            parts = tz[1:].split(":")
            h = int(parts[0])
            minute = int(parts[1])
        else:
            if len(tz) != 5:
                raise _invalid(tz)
            h = int(tz[1:3])
            minute = int(tz[3:5])
        secs = (h * 60 + minute) * 60
        return timezone(timedelta(seconds=-secs if sign == "-" else secs))
    except RuntimeEvaluationError:
        raise
    except Exception:
        raise _invalid(tz) from None


def normalize_offset_in_timestamp(timestamp: str) -> str:
    """Normalises a bare +-HHMM suffix at the end of a timestamp string to
    +-HH:MM so that fromisoformat-style parsing can accept it. Returns the
    original string if no normalisation is needed."""
    n = len(timestamp)
    if n >= 1 and timestamp[-1] in ("Z", "z"):
        return timestamp
    sign_pos = -1
    for i in range(n - 1, -1, -1):
        c = timestamp[i]
        if c in ("+", "-"):
            sign_pos = i
            break
        if not (c.isdigit() or c == ":"):
            break
    if sign_pos < 0:
        return timestamp
    tz = timestamp[sign_pos:]
    if ":" in tz:
        return timestamp
    if len(tz) == 5:
        normalised = tz[:3] + ":" + tz[3:]
        return timestamp[:sign_pos] + normalised
    return timestamp
