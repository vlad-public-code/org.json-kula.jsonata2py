"""Adversarial compile inputs must fail inside the documented error
hierarchy, and the caches they feed must stay bounded.

compile() is routinely handed untrusted expression text (a rules field in a
config, a request body, the argument of $eval). Two things follow:

  * Nothing may escape it except JsonataCompilationError. The parser, the
    optimizer and the translator all walk the tree recursively, and CPython's
    own compiler recurses over the generated source, so a deeply nested or
    very long expression used to surface as a raw RecursionError -- and a
    constant folded to infinity as a raw OverflowError from math.floor(inf).

  * No cache it populates may grow without bound. The factory caches were
    deliberately bounded for this reason; the process-wide compiled-regex
    cache had been missed, and $eval makes regex literals dynamic too.

The nesting limit is host-dependent (it moves with sys.setrecursionlimit),
so these tests assert the *error type*, never a specific depth.
"""

from __future__ import annotations

import pytest

import jsonata2py as jsonata
from jsonata2py.errors import JsonataCompilationError
from jsonata2py.runtime.strings import regex_ops

_FACTORY = jsonata.JsonataExpressionFactory()


@pytest.mark.parametrize(
    "expr",
    [
        pytest.param("(" * 2000 + "1" + ")" * 2000, id="nested-parens"),
        pytest.param("[" * 3000 + "1" + "]" * 3000, id="nested-arrays"),
        pytest.param("+".join(["1"] * 20000), id="flat-binary-chain"),
        pytest.param("1?" * 3000 + "2" + ":3" * 3000, id="chained-ternary"),
        pytest.param("$f := function($x){" * 500 + "1" + "};" * 500 + "1", id="nested-lambdas"),
    ],
)
def test_deeply_nested_expression_raises_compilation_error(expr: str) -> None:
    """A flat "1+1+...+1" chain is in this list on purpose: it parses fine
    (the binary parser loops rather than recurses) and then overflows the
    *optimizer* walking its 20000-deep left-nested AST. That is why the guard
    lives at the pipeline boundary and not as a nesting counter in the parser,
    which could not see this shape at all."""
    with pytest.raises(JsonataCompilationError):
        _FACTORY.compile(expr)


@pytest.mark.parametrize(
    "expr",
    [
        pytest.param("(" * 2000 + "1" + ")" * 2000, id="nested-parens"),
        pytest.param("+".join(["1"] * 20000), id="flat-binary-chain"),
    ],
)
def test_translate_also_maps_recursion_to_compilation_error(expr: str) -> None:
    with pytest.raises(JsonataCompilationError):
        _FACTORY.translate(expr)


def test_module_default_compile_maps_it_too() -> None:
    with pytest.raises(JsonataCompilationError):
        jsonata.compile("(" * 2000 + "1" + ")" * 2000)


def test_factory_still_usable_after_a_too_deep_expression() -> None:
    """The RecursionError is caught with the stack already unwound, so the
    factory must not be left in a broken state."""
    with pytest.raises(JsonataCompilationError):
        _FACTORY.compile("(" * 2000 + "1" + ")" * 2000)
    assert _FACTORY.compile("1 + 2 * 3").evaluate({}) == 7


def test_regex_literal_cache_stays_bounded() -> None:
    """$eval makes expression text -- and so regex literals -- dynamic, so an
    unbounded process-wide cache would grow for the life of the process."""
    distinct = regex_ops._REGEX_CACHE_LIMIT * 4
    for i in range(distinct):
        assert _FACTORY.compile(f'$match("a{i}", /a{i}[0-9]*/)').evaluate({}) is not None
    assert len(regex_ops._CACHE) <= regex_ops._REGEX_CACHE_LIMIT


def test_over_long_regex_literal_is_not_cached() -> None:
    """The cache is keyed on the pattern text, and a pattern can come out of
    an input document, so the KEY length is attacker controlled: bounding the
    entry count alone still lets 512 huge patterns pin unbounded memory. An
    over-long pattern is compiled and used, but never retained."""
    before = len(regex_ops._CACHE)
    filler = "a" * (regex_ops._MAX_CACHEABLE_PATTERN + 1)
    for i in range(32):
        expr = _FACTORY.compile(f'$match("{filler}{i}", /{filler}{i}/)')
        assert expr.evaluate({})["match"] == f"{filler}{i}"
    assert len(regex_ops._CACHE) == before


def test_over_long_regex_literal_behaves_identically_to_a_cached_one() -> None:
    """Only the caching changes at the limit -- never the semantics. The
    dialect rewrites (`.`, `^`/`$` under /m) must fire either side of it."""
    short = "a.b$"
    long = "a" * regex_ops._MAX_CACHEABLE_PATTERN + "|a.b$"
    assert len(short) <= regex_ops._MAX_CACHEABLE_PATTERN < len(long)
    for pattern in (short, long):
        assert _FACTORY.compile(f'$contains("a.b", /{pattern}/)').evaluate({}) is True
        assert _FACTORY.compile(f'$contains("axb\\n", /{pattern}/)').evaluate({}) is False
        assert _FACTORY.compile(f'$contains("axb\\n", /{pattern}/m)').evaluate({}) is True
        assert _FACTORY.compile(f'$contains("a\\rb", /{pattern}/)').evaluate({}) is False


def test_regex_still_works_after_its_cache_evicts() -> None:
    for i in range(regex_ops._REGEX_CACHE_LIMIT * 2):
        _FACTORY.compile(f'$match("b{i}", /b{i}/)').evaluate({})
    assert _FACTORY.compile('$match("ababa", /a/).index').evaluate({}) == [0, 2, 4]
    assert _FACTORY.compile('$replace("abc", /b/, "X")').evaluate({}) == "aXc"
