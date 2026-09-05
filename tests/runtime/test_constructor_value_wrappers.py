"""A constructor's array through the `[]` and `^(...)` wrappers (strand B4).

The third file in the `consarray` set, after `test_array_constructor_path_head.py`
(the constructor **heads** a path) and `test_constructor_step_flattening.py` (it is
a **step** of one). This one pins what the wrappers around such a path do to the
value it produces.

The array a constructor builds is a `cons` value: a single value that happens to
be an array, not a sequence of one. Two wrappers have to know that.

`evaluatePath`, at the end:

```js
if (expr.keepSingletonArray) {
    if (Array.isArray(resultSequence) && resultSequence.cons && !resultSequence.sequence) {
        resultSequence = environment.base.createSequence(resultSequence);
    }
    resultSequence.keepSingleton = true;
}
```

so `[]` **promotes** a cons array rather than passing it through -- `a.[1][]` is
`[[1]]` -- and the `keepSingleton` flag it sets then survives further steps and a
sort. A sort leaves the value cons, and a cons value is not a sequence, so the
final collapse does not apply to it either: `a.[1]^(x)` is `[1]`, not `1`.

Every expectation was produced by running the expression through the reference at
`c:/vlad-projects/js/jsonata` (2.2.2). On the 56-case B4 corpus jsonata2py matches
the reference on all of them, jsonata2js on 41 and jsonata-jvm-compiler on 24, so
these are *not* cross-checked expectations -- the reference is the only arbiter,
and the siblings disagree with it in both directions.
"""

from __future__ import annotations

import pytest

import jsonata2py as jsonata
from jsonata2py.errors import JsonataEvaluationError
from jsonata2py.runtime.core import constructor_step_keep_singleton, force_array_cons
from jsonata2py.runtime.values import MISSING

DATA = {
    "nums": [1, 2, 3],
    "empty": [],
    "objs": [{"x": 1}, {"x": 2}],
    "one": [{"x": 1}],
    "a": {"b": 1, "c": [4, 5]},
}


def ev(expr: str, data: object = DATA) -> object:
    return jsonata.JsonataExpressionFactory().compile(expr).evaluate(data)


class TestForceArrayPromotesAConsValue:
    """`[]` wraps a constructor's array instead of passing it through."""

    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            ("a.[][]", [[]]),
            ("a.[1][]", [[1]]),
            ("a.[1,2][]", [[1, 2]]),
            ("a.[nope][]", [[]]),
            ("a.[[]][]", [[[]]]),
            ("$.[1][]", [[1]]),
            ("a.[1][][]", [[1]]),
        ],
    )
    def test_a_non_sequence_context_is_promoted(self, expr: str, expected: object) -> None:
        assert ev(expr) == expected

    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            ("one.[][]", [[]]),
            ("one.[1][]", [[1]]),
            ("objs.[][]", [[], []]),
            ("nums.[1][]", [[1], [1], [1]]),
        ],
    )
    def test_a_sequence_context_is_already_a_sequence(self, expr: str, expected: object) -> None:
        """Which branch produced the value *is* the cons/sequence distinction:
        by value both are lists, which is why the promotion has to happen
        inside the step helper rather than in a wrapper around it."""
        assert ev(expr) == expected

    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            ("empty.[1][]", MISSING),
            ("a.([1])[]", [1]),  # parenthesised -- not cons, so no promotion
            ("a.{}[]", [{}]),  # object constructors are not affected
            ("nums.{}[]", [{}, {}, {}]),
            ("a.b[]", [1]),
            ("nums[]", [1, 2, 3]),
        ],
    )
    def test_neighbours_are_untouched(self, expr: str, expected: object) -> None:
        result = ev(expr)
        if expected is MISSING:
            assert result is MISSING
        else:
            assert result == expected


class TestSortLeavesAConsValueAlone:
    """Sorting does not make a cons array a sequence, so the collapse that
    turns a one-element sequence into its element does not apply."""

    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            ("a.[1]^(x)", [1]),
            ("a.[nope]^(x)", []),
            ("a.[]^(x)", []),
            ("one.[1]^($)", [1]),
            ("a.[1,2]^($)", [1, 2]),
        ],
    )
    def test_a_constructor_result_survives_the_sort(self, expr: str, expected: object) -> None:
        result = ev(expr)
        assert result is not MISSING
        assert result == expected

    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            ("a.b[]^($)", [1]),
            ("one.x[]^($)", [1]),
            ("a.[1][]^($)", [[1]]),
            ("a.c[]^($)", [4, 5]),
        ],
    )
    def test_keep_singleton_survives_the_sort(self, expr: str, expected: object) -> None:
        """`[]` sets the reference's `keepSingleton` flag on the result, and
        the flag outlives the sort -- so a `[]`-wrapped source never collapses
        back to a scalar."""
        assert ev(expr) == expected

    @pytest.mark.parametrize(("expr", "expected"), [("a.([1])^($)", 1), ("a.{}^($)", {})])
    def test_a_non_cons_source_still_collapses(self, expr: str, expected: object) -> None:
        assert ev(expr) == expected


class TestConsSurvivesAContextRefStep:
    """`$` rebinds the context to itself, so a constructor's array passes
    through it still cons -- and a `[]` after it still promotes."""

    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            ("a.[1].$", [1]),
            ("a.[1].$[]", [[1]]),
            ("one.[1].$[]", [[1]]),
            ("a.[].$[]", [[]]),
            ("a.[1][].$", [[1]]),
            ("a.[1].$^($)", [1]),
            ("nums.[1].$[]", [[1], [1], [1]]),
        ],
    )
    def test_trailing_context_refs_are_transparent(self, expr: str, expected: object) -> None:
        assert ev(expr) == expected

    @pytest.mark.parametrize(("expr", "expected"), [("a.b[].$", [1]), ("a.c[].$", [4, 5])])
    def test_ordinary_paths_are_unaffected(self, expr: str, expected: object) -> None:
        assert ev(expr) == expected


class TestConsFromASubPathShortCircuit:
    """The empty array a constructor-headed sub-path short-circuits to is a
    cons value too, so the wrappers treat it the same way."""

    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            ("a.([].x)[]", [[]]),
            ("one.([].x)[]", [[]]),
            ("a.([nope].x)[]", [[]]),
            ("a.([].x)^($)", []),
            ("a.([].x).$", []),
        ],
    )
    def test_the_short_circuited_array_is_promoted(self, expr: str, expected: object) -> None:
        result = ev(expr)
        assert result is not MISSING
        assert result == expected

    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            ("nums.([].x)[]", [[], [], []]),
            ("objs.([].x)[]", [[], []]),
            ("a.([1,2].$)[]", [1, 2]),
        ],
    )
    def test_a_sequence_of_them_is_not_promoted(self, expr: str, expected: object) -> None:
        assert ev(expr) == expected


class TestALoneElementIsNeverSorted:
    """A separate pre-existing bug, surfaced by the promotion above: the
    reference merge-sorts, so with nothing to compare it never evaluates a key
    and never reports a bad one. Validating a lone element turned working
    expressions into errors."""

    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            ("[[1]]^($)", [1]),
            ("[[1]]^(>$)", [1]),
            ('[{"k":1}]^($)', {"k": 1}),
            ('[{"k":true}]^(k)', {"k": True}),
            ("one^($)", {"x": 1}),
            ("$sort([[1]])", [[1]]),
            ('$sort([{"k":1}])', [{"k": 1}]),
        ],
    )
    def test_no_key_is_evaluated(self, expr: str, expected: object) -> None:
        assert ev(expr) == expected

    @pytest.mark.parametrize(
        ("expr", "code"),
        [
            ('[{"k":true},{"k":false}]^(k)', "T2008"),
            ('[{"k":1},{"k":"a"}]^(k)', "T2007"),
            ("$sort([[1],[2]])", "D3070"),
        ],
    )
    def test_two_elements_still_report(self, expr: str, code: str) -> None:
        """The validation is not removed, only deferred to where the reference
        actually performs a comparison."""
        with pytest.raises(JsonataEvaluationError) as e:
            ev(expr)
        assert e.value.error_code == code


class TestWrapperHelpers:
    """The two new runtime helpers in isolation."""

    def test_keep_singleton_wraps_a_non_sequence_context(self) -> None:
        assert constructor_step_keep_singleton({"a": 1}, lambda _: []) == [[]]
        assert constructor_step_keep_singleton({"a": 1}, lambda _: [1]) == [[1]]

    def test_keep_singleton_leaves_a_sequence_alone(self) -> None:
        assert constructor_step_keep_singleton([1, 2], lambda _: []) == [[], []]
        assert constructor_step_keep_singleton([1], lambda _: [7]) == [[7]]

    def test_keep_singleton_on_an_absent_or_empty_context(self) -> None:
        assert constructor_step_keep_singleton(MISSING, lambda _: []) is MISSING
        assert constructor_step_keep_singleton(None, lambda _: []) is MISSING
        assert constructor_step_keep_singleton([], lambda _: []) is MISSING

    def test_force_array_cons_promotes_only_an_empty_array(self) -> None:
        assert force_array_cons([]) == [[]]
        assert force_array_cons([1, 2]) == [1, 2]
        assert force_array_cons(1) == [1]
        assert force_array_cons(MISSING) is MISSING
