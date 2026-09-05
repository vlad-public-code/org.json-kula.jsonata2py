"""An object built from literal keys skips the duplicate check.

`object_of` guards every write with `if k in result: raise D1009`. When every
key is a string literal, whether two of them collide is a property of the
expression *text* -- decidable once, at compile time, instead of on every
evaluation. The translator emits `object_of_distinct` when it has decided, and
keeps the checking builder when the keys really do repeat or when any key is
computed.

This is the Python-shaped half of `jsonata-performance.md` §5. The Java half
does not port: a `dict` literal is already one allocation with no per-key node,
so there is nothing there to save. The check itself is real though -- a dict
lookup per key -- and §5's own measurement lesson was that keeping it costs
back exactly what the rest of the change saves.
"""

from __future__ import annotations

import pytest

import jsonata2py as jsonata
from jsonata2py.runtime.core import object_of, object_of_distinct
from jsonata2py.runtime.values import MISSING

_FACTORY = jsonata.JsonataExpressionFactory()


def src(expr: str) -> str:
    return _FACTORY.translate(expr)


class TestWhichBuilderIsEmitted:
    @pytest.mark.parametrize(
        "expr",
        [
            "{'a': 1, 'b': 2}",
            "{'a': 1}",
            "items.{'n': name, 'p': price}",
            "{'a': 1, 'b': 2, 'c': 3, 'd': 4}",
        ],
    )
    def test_distinct_literal_keys_skip_the_check(self, expr: str) -> None:
        assert "object_of_distinct(" in src(expr)

    def test_repeated_literal_keys_keep_the_check(self) -> None:
        """The collision is visible in the text, so the checking builder is
        what must be emitted -- D1009 is still the answer."""
        text = src("{'a': 1, 'a': 2}")
        assert "object_of_distinct(" not in text
        assert "object_of(" in text

    def test_a_computed_key_keeps_the_general_builder(self) -> None:
        """Nothing is decidable at compile time when a key is an expression."""
        text = src("($k := 'a'; {$k: 1, 'a': 2})")
        assert "object_of_distinct(" not in text


class TestD1009StillFires:
    """Removing a check is only safe if the error it raised is unreachable."""

    @pytest.mark.parametrize(
        "expr",
        [
            "{'a': 1, 'a': 2}",
            "($k := 'a'; {$k: 1, 'a': 2})",
            "($k := 'a'; $j := 'a'; {$k: 1, $j: 2})",
            "items{name: $.price, 'a': 1}",
        ],
    )
    def test_duplicate_keys_still_raise(self, expr: str) -> None:
        data = {"items": [{"name": "a", "price": 1}]}
        with pytest.raises(jsonata.JsonataEvaluationError) as e:
            _FACTORY.compile(expr).evaluate(data)
        assert e.value.error_code == "D1009"


class TestObjectOfDistinctHelper:
    def test_builds_the_same_object(self) -> None:
        keys, values = ["a", "b", "c"], [1, 2, 3]
        assert object_of_distinct(keys, values) == object_of(keys, values)

    def test_an_unpopulated_slot_is_skipped_but_json_null_is_kept(self) -> None:
        """MISSING is an absent value; None is JSON null and a real one."""
        assert object_of_distinct(["a", "b", "c"], [1, MISSING, None]) == {"a": 1, "c": None}

    def test_insertion_order_is_preserved(self) -> None:
        assert list(object_of_distinct(["z", "a", "m"], [1, 2, 3])) == ["z", "a", "m"]

    def test_empty(self) -> None:
        assert object_of_distinct([], []) == {}
