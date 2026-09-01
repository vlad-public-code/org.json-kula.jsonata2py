"""Epoch-millisecond <-> datetime conversion that is total over the whole
representable range.

`datetime.fromtimestamp()` / `datetime.timestamp()` delegate to the platform
C library, which on Windows rejects every negative timestamp: every
`$fromMillis` of a pre-1970 instant raised instead of formatting, and that
single root cause broke 30+ picture-string cases (`$fromMillis(-2208988800000)`
must give "1900-01-01T00:00:00.000Z").

`timedelta` arithmetic from a fixed epoch has no platform dependency and no
sign asymmetry, which is also how jsonata2js's `datetime.js` stays total (it
does the civil-date arithmetic itself rather than calling a platform routine).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, tzinfo

EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

# datetime spans years 1..9999; these are the epoch-millisecond bounds of
# that range. JavaScript's Date spans a wider +-8.64e15 ms, so a value
# outside this window is reported rather than silently wrapped.
MIN_MILLIS = -62135596800000
MAX_MILLIS = 253402300799999


def to_datetime(millis: int, zone: tzinfo = UTC) -> datetime:
    """The instant `millis` after the epoch, as an aware datetime in `zone`."""
    return (EPOCH + timedelta(milliseconds=millis)).astimezone(zone)


def to_millis(dt: datetime) -> int:
    """Epoch milliseconds for an aware (or UTC-assumed naive) datetime.

    Computed from the timedelta's own normalised fields, which are exact
    integers: timedelta keeps `seconds`/`microseconds` non-negative and
    carries the sign on `days`, so this is correct for pre-epoch instants
    without a sign correction.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    delta = dt - EPOCH
    return delta.days * 86_400_000 + delta.seconds * 1000 + delta.microseconds // 1000


def in_range(millis: int) -> bool:
    return MIN_MILLIS <= millis <= MAX_MILLIS
