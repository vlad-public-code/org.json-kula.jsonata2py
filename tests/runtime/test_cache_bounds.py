"""Every cache in the library must be bounded in MEMORY, not just in entries.

`lru_cache(maxsize=N)` bounds how many entries are retained, not how large
they are. Picture strings, regex patterns and `$eval` expression text all
reach the library from input data, so their length is caller-controlled: a
256-entry cache of 200 KB keys retains ~51 MB, per cache, from one hostile
document. Measured before the guards existed: 60 distinct 200 KB pictures
retained 12.3 MB.

These tests pin both halves of the contract -- oversized input is not
cached, and ordinary input still is -- so a future change cannot quietly
drop either.
"""

from __future__ import annotations

import contextlib

import pytest

import jsonata2py as jsonata
from jsonata2py.runtime.datetime import picture_formatter, picture_parser
from jsonata2py.runtime.numeric import decimal_picture, integer_picture
from jsonata2py.runtime.strings import regex_ops

# (cached function, a picture that is valid but far over the length guard,
#  a short valid picture of the same shape)
_PICTURE_CACHES = [
    pytest.param(integer_picture._analyse_cached, integer_picture.analyse,
                 "0" * 400, "000", id="integer_picture.analyse"),
    pytest.param(picture_formatter._cached__compile_picture, picture_formatter._compile_picture,
                 "[Y0001]" + "x" * 400, "[Y0001]", id="picture_formatter._compile_picture"),
    pytest.param(picture_parser._cached__picture_info, picture_parser._picture_info,
                 "[Y0001]" + "x" * 400, "[Y0001]", id="picture_parser._picture_info"),
]


@pytest.mark.parametrize("cached,entry,long_pic,short_pic", _PICTURE_CACHES)
def test_oversized_picture_is_not_cached(cached, entry, long_pic: str, short_pic: str) -> None:
    cached.cache_clear()
    for i in range(20):
        with contextlib.suppress(Exception):  # an invalid picture must not cache either
            entry(long_pic + str(i))
    assert cached.cache_info().currsize == 0


@pytest.mark.parametrize("cached,entry,long_pic,short_pic", _PICTURE_CACHES)
def test_ordinary_picture_is_still_cached(cached, entry, long_pic: str, short_pic: str) -> None:
    cached.cache_clear()
    for _ in range(5):
        entry(short_pic)
    info = cached.cache_info()
    assert info.currsize == 1
    assert info.hits >= 4


def test_oversized_picture_still_produces_the_same_result() -> None:
    """The guard changes caching only -- never the answer."""
    factory = jsonata.JsonataExpressionFactory()
    expr = factory.compile("$formatInteger(12, pic)")
    short = expr.evaluate({"pic": "000"})
    long_equivalent = expr.evaluate({"pic": "0" * 400})
    assert short == "012"
    assert long_equivalent == "0" * 398 + "12"


def test_decimal_picture_cache_is_guarded() -> None:
    decimal_picture._analyse_picture_cached.cache_clear()
    for i in range(20):
        with contextlib.suppress(Exception):
            decimal_picture._analyse_picture("#" * 400 + str(i), ())
    assert decimal_picture._analyse_picture_cached.cache_info().currsize == 0


def test_regex_cache_is_guarded() -> None:
    regex_ops._CACHE.clear()
    for i in range(20):
        regex_ops.regex_value("a" * 2000 + str(i), "")
    assert len(regex_ops._CACHE) == 0
    regex_ops.regex_value("abc", "")
    assert len(regex_ops._CACHE) == 1


def test_regex_cache_entry_count_is_bounded() -> None:
    regex_ops._CACHE.clear()
    for i in range(regex_ops._REGEX_CACHE_LIMIT + 200):
        regex_ops.regex_value(f"pat{i}", "")
    assert len(regex_ops._CACHE) <= regex_ops._REGEX_CACHE_LIMIT


def test_oversized_regex_still_matches() -> None:
    """As everywhere else, the guard changes caching only."""
    long_pattern = "(?:a)" * 300 + "b"
    value = regex_ops.regex_value(long_pattern, "")
    assert len(long_pattern) > regex_ops._MAX_CACHEABLE_PATTERN
    assert regex_ops.test_match(value, "a" * 300 + "b") is True
    assert regex_ops.test_match(value, "a" * 299 + "b") is False


def test_oversized_picture_regex_is_rejected_not_compiled() -> None:
    """$toMillis is the one place a picture becomes a COMPILED regex, so it
    is the one place the picture's size escapes this library's own caches
    into the stdlib's `re._cache` -- bounded at 512 entries, but not by
    their size. Measured before the cap: 40 pictures of 200 KB left 64 MB
    there. A real picture is tens of characters."""
    factory = jsonata.JsonataExpressionFactory()
    expr = factory.compile("$toMillis('2024-01-01', pic)")
    assert expr.evaluate({"pic": "[Y0001]-[M01]-[D01]"}) == 1704067200000
    with pytest.raises(jsonata.JsonataEvaluationError) as e:
        expr.evaluate({"pic": "[Y0001]" + "z" * 200_000})
    assert e.value.error_code == "D3135"


class TestFactoryCaches:
    """The factory's caches are bounded by BOTH an entry count and the total
    size of the expression text they key on."""

    def test_eval_cache_is_byte_bounded(self) -> None:
        from jsonata2py import factory as factory_mod

        factory = jsonata.JsonataExpressionFactory()
        evaluator = factory.compile("$eval(q)")
        for i in range(120):
            evaluator.evaluate({"q": f"1+{i}" + " " * 40_000})
        assert len(factory._eval_cache) <= factory_mod._EVAL_CACHE_LIMIT
        assert factory._eval_cache_bytes <= factory_mod._EVAL_CACHE_MAX_BYTES
        # the counter must track the contents, or eviction drifts
        assert factory._eval_cache_bytes == sum(len(k) for k in factory._eval_cache)

    def test_compile_cache_is_byte_bounded(self) -> None:
        from jsonata2py import factory as factory_mod

        factory = jsonata.JsonataExpressionFactory()
        keep = []
        for i in range(400):
            keep.append(factory.compile(f"$sum([{i}]) /* {'x' * 3000} */"))
        assert len(factory._compile_cache) <= factory_mod._COMPILE_CACHE_LIMIT
        assert factory._compile_cache_bytes <= factory_mod._COMPILE_CACHE_MAX_BYTES
        assert factory._compile_cache_bytes == sum(len(k) for k in factory._compile_cache)

    def test_code_cache_is_byte_bounded(self) -> None:
        from jsonata2py import factory as factory_mod

        factory = jsonata.JsonataExpressionFactory()
        for i in range(200):
            factory.compile(f"$sum([{i}]) /* {'y' * 2000} */")
        assert len(factory._code_cache) <= factory_mod._CODE_CACHE_LIMIT
        assert factory._code_cache_bytes <= factory_mod._CODE_CACHE_MAX_BYTES
