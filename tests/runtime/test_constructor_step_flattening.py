"""When an array constructor is a path *step*: flattening and the terminal rule.

The companion to `test_array_constructor_path_head.py`. That file pins what
happens when a constructor **heads** a path; this one pins what happens when it
sits anywhere else in one, which turns on the same reference mechanism seen from
the other end.

`src/parser.js` flags a constructor at **both** ends of a path:

```js
var firststep = result.steps[0];
if (firststep.type === 'unary' && firststep.value === '[') {
    firststep.consarray = true;                    // heads a path
}
// if the last step is an array constructor, flag it so it doesn't flatten
var laststep = result.steps[result.steps.length - 1];
if (laststep.type === 'unary' && laststep.value === '[') {
    laststep.consarray = true;                     // ends one
}
```

and `jsonata.js:555` attaches the runtime `cons` property -- "do not flatten me,
do not collapse me" -- **only** when that flag is set. So the array a
constructor builds behaves differently at exactly two syntactic positions and
nowhere else. Three consequences, one test group each:

1. **Parenthesising defeats it.** `nums.[1,2]` is three arrays;
   `nums.([1,2])` flattens to six numbers. Exactly the same rule as
   `[].x` versus `([]).x`, at the other end of the path.
2. **A terminal step passes a single array result through verbatim**
   (`jsonata.js:270`), so `a.[]` is `[]` rather than undefined and `a.[1]` is
   `[1]` rather than `1`.
3. **A `$` step is a real step.** `[1].$` is `1`: the step produces a
   one-element sequence, which collapses.

Every expectation here was produced by running the expression through the
reference at `c:/vlad-projects/js/jsonata` (2.2.2) and cross-checked against
jsonata2js, which agrees with the reference on all but one of them. The
official suite covers none of it.
"""

from __future__ import annotations

import pytest

import jsonata2py as jsonata
from jsonata2py.runtime.core import constructor_step_final, step_final
from jsonata2py.runtime.values import MISSING

DATA = {
    "nums": [1, 2, 3],
    "empty": [],
    "objs": [{"x": 1}, {"x": 2}],
    "one": [{"x": 1}],
    "a": {"b": 1, "c": [4, 5]},
    "tw": [5, 6],
    "single": [1],
}


def ev(expr: str, data: object = DATA) -> object:
    return jsonata.JsonataExpressionFactory().compile(expr).evaluate(data)


class TestParenthesisingDefeatsTheNoFlattenRule:
    """A bare `[...]` step keeps its array; a parenthesised one flattens.

    The optimizer used to strip the `Parenthesized` wrapper, making the two
    indistinguishable -- the same mistake §6.2 of the conformance note warns
    about for the path *head*, in the opposite direction.
    """

    @pytest.mark.parametrize(
        ("bare", "parenthesised", "kept", "flattened"),
        [
            ("nums.[1,2]", "nums.([1,2])", [[1, 2], [1, 2], [1, 2]], [1, 2, 1, 2, 1, 2]),
            ("nums.[$]", "nums.([$])", [[1], [2], [3]], [1, 2, 3]),
            ("nums.[[1,2]]", "nums.([[1,2]])", [[[1, 2]], [[1, 2]], [[1, 2]]], [[1, 2], [1, 2], [1, 2]]),
        ],
    )
    def test_the_two_spellings_differ(self, bare: str, parenthesised: str, kept: object, flattened: object) -> None:
        assert ev(bare) == kept
        assert ev(parenthesised) == flattened

    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            ('nums.(["a","b"])', ["a", "b", "a", "b", "a", "b"]),
            ("[1,2].([3,4])", [3, 4, 3, 4]),
            ("[1,2].([3,4].$)", [3, 4, 3, 4]),
            ("nums.([1,2].$)", [1, 2, 1, 2, 1, 2]),
            ("nums.([1,2]).$", [1, 2, 1, 2, 1, 2]),
            ("nums.([1,2,3].$)", [1, 2, 3, 1, 2, 3, 1, 2, 3]),
            ("nums.([1,2,3].$string())", ["1", "2", "3", "1", "2", "3", "1", "2", "3"]),
            ("$count(nums.([1,2,3].$))", 9),
            ("$sum(nums.([1,2,3].$))", 18),
        ],
    )
    def test_a_parenthesised_constructor_step_flattens(self, expr: str, expected: object) -> None:
        assert ev(expr) == expected

    def test_an_empty_parenthesised_constructor_contributes_nothing(self) -> None:
        """Three empty arrays flattened into the parent leave nothing, so the
        path collapses -- where the bare form keeps all three."""
        assert ev("nums.([])") is MISSING
        assert ev("nums.[]") == [[], [], []]

    def test_it_is_not_cosmetic(self) -> None:
        """`$sum` over three arrays is a type error; over nine numbers it is a
        number. The divergence made a working expression throw."""
        assert ev("$sum(nums.([1,2,3].$))") == 18


class TestTerminalStepPassesOneArrayThrough:
    """`jsonata.js:270` -- a path's final step over a single-element input,
    yielding one array that is not a sequence, returns that array whole.

    jsonata2py collapsed it instead, turning `[]` into undefined and `[1]`
    into `1`.
    """

    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            ("a.[]", []),
            ("a.[nope]", []),
            ("a.([])", []),
            ("a.[[]]", [[]]),
            ("a.[1]", [1]),
            ("a.([nope,1])", [1]),
            ("a.[1,2]", [1, 2]),
            ("one.($zip(single,tw))", []),
        ],
    )
    def test_single_array_result_is_verbatim(self, expr: str, expected: object) -> None:
        result = ev(expr)
        assert result is not MISSING
        assert result == expected

    def test_an_absent_element_shortens_the_constructor_rather_than_erasing_it(self) -> None:
        """`[nope]` is `[]`, not undefined -- a constructor *drops* an element
        that evaluates to nothing. Compiling it to `force_array` propagated
        the absence instead, so every element fell out."""
        assert ev("a.[nope]") == []
        assert ev("nums.[nope]") == [[], [], []]

    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            ("one.[]", []),
            ("objs.[]", [[], []]),
            ("nums.[]", [[], [], []]),
            ("nums.[[]]", [[[]], [[]], [[]]]),
            ("one.[1]", [1]),
            ("objs.[1]", [[1], [1]]),
            ("a.{}", {}),
            ("a.b", 1),
            ("a.c", [4, 5]),
            ("objs.x", [1, 2]),
        ],
    )
    def test_shapes_that_already_agreed_still_agree(self, expr: str, expected: object) -> None:
        assert ev(expr) == expected

    def test_two_or_more_results_still_flatten_and_collapse(self) -> None:
        """The verbatim rule is `result.length === 1` only. `objs` has two
        elements, so the two empty arrays flatten away and the path collapses
        -- while the one-element `one` keeps its single `[]`."""
        assert ev("objs.(x)") == [1, 2]
        assert ev("objs.($zip(single,tw))") is MISSING
        assert ev("one.($zip(single,tw))") == []


class TestContextRefStepIsARealStep:
    """`$` as the last step maps and collapses like any other step."""

    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            ("[1].$", 1),
            ("[nope,1].$", 1),
            ("[1].($)", 1),
            ("[1,2].$", [1, 2]),
            ("[1,2,3].$", [1, 2, 3]),
            ("[1].$string()", "1"),
        ],
    )
    def test_terminal_context_ref_collapses(self, expr: str, expected: object) -> None:
        assert ev(expr) == expected

    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            ("one.[$].$", [{"x": 1}]),
            ("one.[0].$", [0]),
            ("one.[[]].$", [[]]),
            ("one.[].$", []),
        ],
    )
    def test_a_constructor_step_is_not_collapsed_twice(self, expr: str, expected: object) -> None:
        """A constructor step already finalises its own result. Collapsing it
        again in the following `$` erased one level -- the reference collapses
        once, at the end."""
        result = ev(expr)
        assert result is not MISSING
        assert result == expected


class TestConstructorStepFinalHelper:
    """The terminal constructor-step helper in isolation."""

    def test_a_non_sequence_context_returns_the_constructor_verbatim(self) -> None:
        assert constructor_step_final({"a": 1}, lambda _: []) == []
        assert constructor_step_final({"a": 1}, lambda _: [1]) == [1]

    def test_a_singleton_sequence_collapses_to_that_one_array(self) -> None:
        assert constructor_step_final([{"x": 1}], lambda _: []) == []
        assert constructor_step_final([{"x": 1}], lambda _: [1]) == [1]

    def test_two_or_more_results_are_kept_separate(self) -> None:
        """Never flattened -- that is the whole of the `cons` rule."""
        assert constructor_step_final([1, 2, 3], lambda _: []) == [[], [], []]
        assert constructor_step_final([1, 2], lambda _: [7, 8]) == [[7, 8], [7, 8]]

    def test_absent_results_drop_out(self) -> None:
        assert constructor_step_final([1, 2], lambda v: MISSING if v == 1 else [v]) == [2]

    def test_an_absent_or_empty_context_yields_missing(self) -> None:
        assert constructor_step_final(MISSING, lambda _: []) is MISSING
        assert constructor_step_final(None, lambda _: []) is MISSING
        assert constructor_step_final([], lambda _: []) is MISSING


class TestStepFinalHelper:
    """The generic terminal-step helper in isolation."""

    def test_a_single_array_result_is_not_flattened(self) -> None:
        assert step_final([1], lambda _: []) == []
        assert step_final([1], lambda _: [[7]]) == [[7]]

    def test_two_or_more_results_flatten_and_collapse(self) -> None:
        assert step_final([1, 2], lambda _: [7, 8]) == [7, 8, 7, 8]
        assert step_final([1, 2], lambda v: v) == [1, 2]
        assert step_final([1, 2], lambda v: MISSING if v == 1 else v) == 2

    def test_a_non_sequence_context_is_stepped_once(self) -> None:
        assert step_final({"a": 1}, lambda _: []) == []

    def test_an_absent_or_empty_context_yields_missing(self) -> None:
        assert step_final(MISSING, lambda _: 1) is MISSING
        assert step_final([], lambda _: 1) is MISSING

    def test_a_null_context_is_stepped_like_any_other_value(self) -> None:
        """A JSON `null` is a value, not an absence: only navigation treats
        the two alike. `n.$string($)` over `{"n": null}` is `"null"`."""
        assert step_final(None, lambda v: v) is None
        assert step_final(None, lambda _: 1) == 1
