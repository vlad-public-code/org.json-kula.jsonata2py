"""Five divergence families closed on 2026-09-04 (§13 of the cross-port notes).

Each was found by running a generated path sweep against the reference at
`c:/vlad-projects/js/jsonata` (2.2.2) and reading its source for the rule.
Every expectation below is the reference's own answer, and the neighbouring
row is kept next to the interesting one -- a test that pins only the
interesting case tends to be satisfied by an implementation that has stopped
doing the ordinary one.

The five:

* **`[]` is a mark, not a wrapper.** `keepArray` is read inside `evaluate`'s
  `isSequence(result)` branch, so `[]` after anything that is not a sequence
  does nothing *yet* -- and still fires for the next thing on that node that
  builds one. `1[]` is `1`; `1[][0]` is `[1]`.
* **A JSON `null` survives a stage.** Only navigation treats null and absent
  alike (`null.x` really is undefined); a stage does not, so `n[]` is
  `[null]`.
* **A `$`-valued step spreads an array item.** `$` re-binds the context to
  itself, but the step's results are still finalised, and that spreads a
  plain array: `nested.$` over `[[1,2],[3]]` is `[1,2,3]`.
* **An object constructor is a group expression.** It iterates its input, so
  an array context is grouped over its *elements*, and a group's data is
  accumulated with `fn.append`.
* **A stage on a constructor head.** §1's short-circuit hands the head's
  value to the next step, which iterates it -- so a stage that leaves it a
  scalar or an object makes the whole path undefined.

Plus two rules the sweep reached along the way: a literal may not be a path
step (§11.11), and S0209 is narrower than "a stage after a group-by".
"""

from __future__ import annotations

from typing import ClassVar

import pytest

import jsonata2py as jsonata
from jsonata2py.errors import JsonataCompilationError
from jsonata2py.runtime.values import MISSING

DATA = {
    "a": {"b": 1, "c": [2, 3]},
    "nums": [1, 2, 3],
    "objs": [{"x": 1}, {"x": 2}],
    "nested": [[1, 2], [3]],
    "m": [{"v": [1, 2]}, {"v": [3]}],
    "empty": [],
    "one": [7],
    "n": None,
    "o": {"p": None},
    "arr": [1, None, 3],
}


def ev(expr: str, data: object = DATA) -> object:
    return jsonata.JsonataExpressionFactory().compile(expr).evaluate(data)


class TestEmptyBracketsAreAMark:
    """`[]` fires only on a sequence -- but the mark is not lost."""

    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            # Not a sequence: nothing happens.
            ("1[]", 1),
            ('"z"[]', "z"),
            ("true[]", True),
            ("{}[]", {}),
            ('{"k":1}[]', {"k": 1}),
            ("$sum(nums)[]", 6),
            ("$string(nums)[]", "[1,2,3]"),
            ("(a.b)[]", 1),
            ('$eval("1")[]', 1),
            ("$append(1, nope)[]", 1),
            ("1+1[]", 2),
            # A sequence: it fires, as it always did.
            ("a.b[]", [1]),
            ("nums[]", [1, 2, 3]),
            ("a.*[]", [1, 2, 3]),
            ("nums[0][]", [1]),
            ("$keys(a)[]", ["b", "c"]),
            ("$map(one, function($v){$v})[]", [7]),
            # Held, then fired by the next stage on the same node.
            ("1[][0]", [1]),
            ("1[0][]", [1]),
            ("{}[].$", [{}]),
            ("(a.b)[][0]", [1]),
            # A sort builds a sequence, so a `[]` on the *sort* fires...
            ("1^($)[]", [1]),
            ('"z"^($)[]', ["z"]),
            # ...while one on the literal underneath it still does not, and
            # a mark that does survive a sort does not suppress the
            # length-0 collapse.
            ("1[]^($)", 1),
            ("a.b[]^($)", [1]),
            ("empty[]^($)", MISSING),
        ],
    )
    def test_the_mark_fires_only_on_a_sequence(self, expr: str, expected: object) -> None:
        assert ev(expr) == expected

    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            ("nums ~> $sum()[]", 6),
            ("a.b ~> $string()[]", "1"),
            ("a ~> $string()[0]", '{"b":1,"c":[2,3]}'),
            ("nums ~> $string()^($)", "[1,2,3]"),
        ],
    )
    def test_a_postfix_after_a_chain_step_runs_over_the_applied_result(
        self, expr: str, expected: object
    ) -> None:
        """The reference hangs it on the apply node as a stage, so it is not
        part of the call -- which is also what keeps the call a partial
        application."""
        assert ev(expr) == expected

    def test_the_path_keeps_its_singleton_across_later_steps(self) -> None:
        """keepSingletonArray is read once, at the end of the path."""
        assert ev("nums[][0]") == [1]
        assert ev("nums[][0].$") == [1]
        assert ev("nums[][-1].$") == [3]


class TestANullSurvivesAStage:
    """A JSON `null` is a value; only navigation treats it as an absence."""

    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            ("n", None),
            ("o.p", None),
            ("n[]", [None]),
            ("o.p[]", [None]),
            ("n[0]", None),
            ("n[true]", None),
            ("n.$", None),
            ("n.$[0]", None),
            ("n.$string($)", "null"),
            ('n.{"k":$}', {"k": None}),
            # Navigation is the exception, and stays one.
            ("n.x", MISSING),
            ("nope[]", MISSING),
            # Arrays were never affected.
            ("arr[]", [1, None, 3]),
            ("$count(arr)", 3),
        ],
    )
    def test_null_through_stages_and_steps(self, expr: str, expected: object) -> None:
        assert ev(expr) == expected


class TestAContextStepSpreadsAnArrayItem:
    """`.$` is an identity map, but it is still a step."""

    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            ("nested.$", [1, 2, 3]),
            ("nested.($)", [1, 2, 3]),
            ("nested.$.$", [1, 2, 3]),
            ("nested[].$", [1, 2, 3]),
            # A field step always did this -- the two must agree.
            ("m.v", [1, 2, 3]),
            # A single raw array as the last step's only result passes
            # through, and a constructor's array is cons and never spreads.
            ("nested.[$]", [[1, 2], [3]]),
            ("a.[1].$", [1]),
            ("a.[1][].$", [[1]]),
            # Untouched shapes.
            ("nums.$", [1, 2, 3]),
            ("nested.$string($)", ["[1,2]", "[3]"]),
            ("nested.$[0]", [1, 3]),
        ],
    )
    def test_a_plain_array_item_is_spread(self, expr: str, expected: object) -> None:
        assert ev(expr) == expected


class TestAnObjectConstructorIsAGroupExpression:
    """It iterates its input, and accumulates a group with fn.append."""

    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            # An array item is grouped over its own elements, so a
            # single-element one collapses.
            ('nested.{"k":$}', [{"k": [1, 2]}, {"k": 3}]),
            ('one.{"k":$}', {"k": 7}),
            # Two array items in one group concatenate rather than nest.
            ('nested{"k":$string($)}', {"k": "[1,2,3]"}),
            # Scalars are unchanged, which is the overwhelmingly common case.
            ('nums.{"k":$}', [{"k": 1}, {"k": 2}, {"k": 3}]),
            ('nums{"k":$string($)}', {"k": "[1,2,3]"}),
            ('a.{"k":b}', {"k": 1}),
            # An empty or absent source still runs the pairs once, with an
            # absent context -- so a literal-keyed object is still built.
            ('nope{"k":1}', {"k": 1}),
            ('nope{"k":$string($)}', {}),
            ('empty{"k":1}', {"k": 1}),
            ('nums[false]{"k":1}', {"k": 1}),
        ],
    )
    def test_grouping_over_an_array_context(self, expr: str, expected: object) -> None:
        assert ev(expr) == expected


class TestAStageOnAConstructorHead:
    """§1's short-circuit hands the head's *value* to the next step."""

    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            # No further step: the stage is just a stage.
            ("[1][0]", 1),
            ("[1][0][0]", 1),
            ('[1]{"k":1}', {"k": 1}),
            # A further step iterates the head's value, and a scalar or an
            # object has nothing to iterate.
            ("[1][0].$", MISSING),
            ("[1][0].x", MISSING),
            ("[1,2][0].$", MISSING),
            ("[nums][0].$", MISSING),
            ("[1][true].$", MISSING),
            ('[1]{"k":1}.$', MISSING),
            # The parenthesis is the only thing separating these.
            ("([1])[0].$", 1),
            # An unstaged head is an array, so the rest of the path runs.
            ("[1].$", 1),
            ("[].x", []),
            # ...and the empty array it short-circuits to is a cons value.
            ("[].x[]", [[]]),
            ("[].x^($)", []),
            ("[][].$", [[]]),
            ("[1].$^($)", 1),
        ],
    )
    def test_a_staged_head_stops_the_path(self, expr: str, expected: object) -> None:
        assert ev(expr) == expected


class TestALiteralMayNotBeAPathStep:
    """§11.11: the reference filters the finished step list, so this catches
    the head as well as anything after a dot."""

    @pytest.mark.parametrize(
        "expr",
        ["1.x", "true.x", "null.x", "a.true", "a.null", "a.1", "1[].$", "true[].$", "1[0].$", "1.$"],
    )
    def test_a_literal_step_is_s0213(self, expr: str) -> None:
        with pytest.raises(JsonataCompilationError) as exc:
            ev(expr)
        assert exc.value.error_code == "S0213"

    @pytest.mark.parametrize(
        ("expr", "expected"),
        [("a.(1)", 1), ("a.(1+1)", 2), ("nums.(2)", [2, 2, 2]), ('a."b"', 1), ('"a".b', 1)],
    )
    def test_a_parenthesised_block_stays_legal(self, expr: str, expected: object) -> None:
        """Constant folding legitimately turns `a.(1+1)` into a literal step,
        which is why the check belongs in the parser."""
        assert ev(expr) == expected


class TestS0209IsNarrowerThanAStageAfterAGroupBy:
    """The reference reads the group off the *last step* of a path, and off
    the node itself otherwise."""

    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            ('nums{"k":$string($)}[0]', {"k": "1"}),
            ('a{"k":b}[0]', {"k": 1}),
            ('a{"k":b}[]', {"k": 1}),
            ('empty{"k":$string($)}[0]', {}),
        ],
    )
    def test_a_group_on_a_path_runs_after_the_stage(self, expr: str, expected: object) -> None:
        assert ev(expr) == expected

    @pytest.mark.parametrize("expr", ['[1,2,3]{"num": $}[true]', '[1]{"k":1}[0]'])
    def test_a_group_on_a_non_path_is_s0209(self, expr: str) -> None:
        with pytest.raises(JsonataCompilationError) as exc:
            ev(expr)
        assert exc.value.error_code == "S0209"

    def test_empty_brackets_never_reach_the_check(self) -> None:
        """`[]` sets keepArray in its own production, not through the `[`
        infix that raises S0209."""
        assert ev('[1]{"k":$string($)}[]') == {"k": "1"}


class TestAnIndexShapedPredicateFoldsOntoTheLastStep:
    """`[[0]]` and `[[0,2]]` are documented JSONata for index selection, and
    they scope to the last *step* like a bare numeric subscript does --
    `objs.t[[0]]` is the first `t` of each `objs`, not the first of the
    flattened result.

    Only the index shape is folded. Folding every predicate is what the
    reference does, but doing that here regresses this port's own
    per-element scoping (26 tests) -- and only an index-shaped predicate can
    tell per-step from per-path apart, because a boolean one gives the same
    answer either way.
    """

    DATA: ClassVar[dict[str, object]] = {
        "objs": [{"t": [1, 2]}, {"t": []}, {"t": [3]}],
        "nums": [1, 2, 3],
        "a": {"c": [2, 3]},
    }

    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            ("objs.t[[0]]", [1, 3]),
            ("objs.t[[0,1]]", [1, 2, 3]),
            ("objs.t[[-1]]", [2, 3]),
            # The bare subscript always did this -- the two must agree.
            ("objs.t[0]", [1, 3]),
            # A boolean predicate is unaffected either way.
            ("objs.t[$>1]", [2, 3]),
            # Parenthesising makes it the whole path's, as it does for `[0]`.
            ("(objs.t)[[0]]", 1),
            # A single-step path has only one scope, so nothing changes.
            ("nums[[0,2]]", [1, 3]),
            ("nums[[0]]", 1),
            ("nums[[5]]", MISSING),
            ("a.c[[0]]", 2),
            # ...and it composes with what follows.
            ("objs[[0]].t", [1, 2]),
            ("objs.t[[0]][0]", [1, 3]),
        ],
    )
    def test_index_predicates(self, expr: str, expected: object) -> None:
        assert ev(expr, self.DATA) == expected

    def test_a_predicate_step_navigates_its_own_source(self) -> None:
        """The bug the fold exposed: a PredicateExpr used as a path step
        ignored its `.source` and filtered the incoming stream instead, which
        is why folding put the predicate on the wrong step."""
        src = jsonata.JsonataExpressionFactory().translate("objs.t[[0]]")
        assert "'t'" in src, src
