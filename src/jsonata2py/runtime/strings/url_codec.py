"""Percent-encoding and decoding for $encodeUrl, $encodeUrlComponent,
$decodeUrl, $decodeUrlComponent.

Ported from org.json_kula.jsonata_jvm.runtime.string.UrlCodec.

JSONata follows JavaScript's encodeURI/encodeURIComponent, whose safe
character sets are specific (RFC 3986 unreserved, plus RFC 3986 reserved
for the full-URL variant) -- derived here from the Java source, not from
Python's urllib.parse defaults, which use a different safe set.
"""

from __future__ import annotations

from ...errors import _RuntimeEvaluationError as RuntimeEvaluationError

_HEX = "0123456789ABCDEF"


def _is_unreserved(c: int) -> bool:
    """RFC 3986 unreserved characters: A-Za-z0-9 - _ . ~"""
    return (
        (ord("A") <= c <= ord("Z"))
        or (ord("a") <= c <= ord("z"))
        or (ord("0") <= c <= ord("9"))
        or c in (ord("-"), ord("_"), ord("."), ord("~"))
    )


_RESERVED = set(":/?#[]@!$&'()*+,;=")


def _is_reserved(c: int) -> bool:
    """RFC 3986 reserved characters (kept by $encodeUrl, encoded by
    $encodeUrlComponent)."""
    return chr(c) in _RESERVED


def encode(s: str, preserve_reserved: bool) -> str:
    """Percent-encodes s per RFC 3986.

    preserve_reserved=True keeps RFC 3986 reserved characters unencoded
    (full-URL encoding); False only keeps unreserved characters (component
    encoding).
    """
    # Surrogate validation mirrors the Java check, but Python str already
    # stores full code points (no surrogate pairs), so a lone surrogate can
    # only arrive via surrogateescape-decoded input -- reject it the same way.
    for ch in s:
        cp = ord(ch)
        if 0xD800 <= cp <= 0xDFFF:
            raise RuntimeEvaluationError(
                "D3140", "$encodeUrl/Component: the string contains an invalid Unicode character"
            )
    data = s.encode("utf-8")
    out: list[str] = []
    for b in data:
        if _is_unreserved(b) or (preserve_reserved and _is_reserved(b)):
            out.append(chr(b))
        else:
            out.append(f"%{_HEX[b >> 4]}{_HEX[b & 0xF]}")
    return "".join(out)


def decode(s: str) -> str:
    """Decodes percent-encoded sequences. Raises D3140 on malformed input
    (incomplete sequences, invalid hex digits, or literal non-ASCII
    characters)."""
    out = bytearray()
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if ord(c) > 0x7F:
            raise RuntimeEvaluationError("D3140", f"Malformed URL: non-ASCII character at position {i}")
        if c == "%":
            if i + 2 >= n:
                raise RuntimeEvaluationError("D3140", f"Malformed URL: incomplete percent sequence at position {i}")
            hi = _hex_digit(s[i + 1])
            lo = _hex_digit(s[i + 2])
            if hi < 0 or lo < 0:
                raise RuntimeEvaluationError(
                    "D3140", f"Malformed URL: invalid percent sequence '%{s[i + 1]}{s[i + 2]}'"
                )
            out.append(hi * 16 + lo)
            i += 3
        else:
            out.append(ord(c))
            i += 1
    try:
        return bytes(out).decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise RuntimeEvaluationError("D3140", "Malformed URL: invalid UTF-8 sequence") from None


def _hex_digit(c: str) -> int:
    try:
        return int(c, 16)
    except ValueError:
        return -1
