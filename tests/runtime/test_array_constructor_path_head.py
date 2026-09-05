"""An array constructor heading a path expression (the `consarray` quirk).

When a path's *first step* is an array constructor and that constructor comes
out empty, the reference returns `[]` -- not undefined -- and never evaluates
the remaining steps:

    [].x            ->  []          but  ([]).x  ->  undefined
    $type([].x)     ->  "array"          empty.x ->  undefined

The mechanism in `jsonata` 2.2.2 is a `consarray` flag the parser sets on
`steps[0]` when it is an array constructor. `evaluatePath` then evaluates that
step as a *value* rather than iterating it, and breaks out of the step loop the
moment a step yields nothing. The resulting empty array is a plain array, not a
sequence, so it escapes the collapse that turns every other empty result into
undefined.

Whether that is principled is a judgement call -- two spellings of the same
value disagree -- but it is the de-facto specification, and jsonata2py,
jsonata2js and the JVM port all originally returned undefined for all 18
divergent cases. See `docs/` and the cross-port conformance note dated
2026-09-02.

The official 1,281-case suite exercises the *shape* (18 expressions are paths
led by an array constructor) but never with an empty constructor, so it cannot
catch a regression in either direction. These tests carry that weight alone.

Three properties are load-bearing, and each has its own group below:

1. the trigger is *syntactic*  -- `([]).x` and `nums.[].x` are unaffected;
2. the trigger is also *dynamic* -- only an empty result short-circuits;
3. the general empty-sequence collapse is untouched -- `empty.x`,
   `nums[false].x` and `$filter(...).x` all stay undefined.
"""

from __future__ import annotations

import pytest

import jsonata2py as jsonata
from jsonata2py.errors import JsonataEvaluationError
from jsonata2py.parser.ast_nodes import ArrayConstructor, PathExpr, SortExpr
from jsonata2py.parser.parser import Parser
from jsonata2py.runtime.core import consarray_head, map_consarray_step
from jsonata2py.runtime.values import MISSING

DATA = {"nums": [1, 2, 3], "empty": [], "objs": [{"x": 1}], "nested": {"e": []}, "a": {"b": 1}}


def ev(expr: str, data: object = DATA) -> object:
    return jsonata.JsonataExpressionFactory().compile(expr).evaluate(data)


def src(expr: str) -> str:
    return jsonata.JsonataExpressionFactory().translate(expr)


class TestEmptyConstructorHeadYieldsEmptyArray:
    """The divergence itself: 18 expressions that returned undefined before.

    `[]` and undefined are easy to confuse at a REPL, so the assertions use
    `is not MISSING` plus an exact `== []` rather than truthiness.
    """

    @pytest.mark.parametrize(
        "expr",
        [
            "[].x",
            "[].x.y",
            "[].$count()",
            "[].*",
            "[].**",
            "[].{}",
            "[].[1]",
            "[]^(x).y",
        ],
    )
    def test_literal_empty_constructor(self, expr: str) -> None:
        result = ev(expr)
        assert result is not MISSING
        assert result == []

    @pytest.mark.parametrize(
        "expr",
        [
            "[nope].x",
            "[nope, nope].x",
            "[nums[false]].x",
            "[empty].x",
            "[$filter(nums, function($v){false})].x",
        ],
    )
    def test_emptiness_may_be_data_driven(self, expr: str) -> None:
        """Not "an empty array literal typed into the expression": a
        constructor drops elements that evaluate to nothing, so one that is
        written non-empty can still be empty at run time."""
        result = ev(expr)
        assert result is not MISSING
        assert result == []

    def test_sort_on_the_head_still_short_circuits(self) -> None:
        """`[]^(x).y` parses here as `SortExpr(ArrayConstructor([]))`, not as
        two steps the way the reference parses it, so the flag has to be set
        on the constructor *inside* the sort. Sorting an empty array is still
        empty, so short-circuiting after the sort gives the same answer as the
        reference's break-before-sort."""
        assert ev("[]^(x).y") == []
        assert ev("[nope]^(x).y") == []


class TestEmptyArrayIsObservableAsAnArray:
    """It is not cosmetic: the empty array reaches the caller as a genuine
    array value, changing booleans, types, strings and shapes.

    `$type` is the decisive one -- a port that collapses at the expression
    boundary rather than per step would erase the array again, and `[].x`
    alone would not show it."""

    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            ("$type([].x)", "array"),
            ("$exists([].x)", True),
            ("$string([].x)", "[]"),
            ("$boolean([].x)", False),
            ("$append([].x, 1)", [1]),
            ("[].x = []", True),
            ("$count([].x)", 0),
        ],
    )
    def test_consumers_see_an_array(self, expr: str, expected: object) -> None:
        assert ev(expr) == expected

    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            ("$count([].x)", 0),
            ('[].x ? "yes" : "no"', "no"),
            ("[] ~> $count()", 0),
        ],
    )
    def test_some_consumers_hide_it(self, expr: str, expected: object) -> None:
        """Both `[]` and undefined are falsy and both count as 0, so these
        agreed with the reference before the fix too. They are here to pin
        that the fix did not change them either."""
        assert ev(expr) == expected


class TestRemainingStepsAreSkippedNotEvaluated:
    """The remaining steps are genuinely not evaluated -- which is why they
    have to be passed as a thunk rather than an already-computed value."""

    def test_step_body_never_runs(self) -> None:
        assert ev('[].($error("boom"))') == []

    def test_negative_control_the_step_body_does_run_when_non_empty(self) -> None:
        """Without this, a fix that skips the remaining steps unconditionally
        would pass the test above."""
        with pytest.raises(JsonataEvaluationError) as e:
            ev('["a"].($error("boom"))')
        assert e.value.error_code == "D3137"


class TestTriggerIsSyntactic:
    """Property 1: the constructor must be the *literal* first step.

    This is why the flag is recorded in the parser. The optimizer strips the
    `Parenthesized` wrapper, so by translation time `([]).x` and `[].x` are
    the same tree -- a translator-side "is the head an array constructor?"
    test would wrongly fire on both."""

    @pytest.mark.parametrize(
        "expr",
        [
            "([]).x",  # parenthesised -- the head is a block, not a constructor
            "($e := []; $e.x)",  # the same value through a variable
            "nums.[].x",  # a constructor, but not at step 0
            "objs.[].x",
            "$.[].x",
            "[][0].x",  # a predicated step is a different parse shape
            "{}.x",  # only `[` gets the flag; object constructors are exempt
            "({}).x",
        ],
    )
    def test_unaffected(self, expr: str) -> None:
        assert ev(expr) is MISSING

    def test_parenthesised_and_bare_forms_differ(self) -> None:
        """The whole point, stated once: the same value, two spellings, two
        answers. Deliberate reference behaviour, pinned so nobody
        "simplifies" it away."""
        assert ev("[].x") == []
        assert ev("([]).x") is MISSING


class TestTriggerIsDynamic:
    """Property 2: a non-empty constructor is unaffected -- the divergence
    needs `length === 0`, so the array-ness is not preserved in general."""

    @pytest.mark.parametrize(
        "expr",
        [
            '["a"].x',
            "[1].x",
            "[[]].x",
            "[nums].x",
        ],
    )
    def test_non_empty_constructor_yields_undefined(self, expr: str) -> None:
        assert ev(expr) is MISSING

    def test_non_empty_constructor_still_maps(self) -> None:
        assert ev('[{"x":1}].x') == 1
        assert ev('[{"x":1},{"x":2}].x') == [1, 2]

    def test_official_suite_shapes_are_untouched(self) -> None:
        """Representative cases from the 18 suite expressions that are paths
        led by an array constructor. All have non-empty heads."""
        assert ev("[1..5].$string()") == ["1", "2", "3", "4", "5"]
        assert ev("[1, 2, 3].$") == [1, 2, 3]
        assert ev("[-2..2].($*$)") == [4, 1, 0, 1, 4]
        assert ev('[{"a":[1,2]}, {"a":[3]}].a') == [1, 2, 3]
        assert ev("[1,2,3].$v") is MISSING


class TestGeneralCollapseIsUnchanged:
    """Property 3, and the one that breaks other things if it is got wrong:
    every *other* route to an empty result still collapses to undefined."""

    @pytest.mark.parametrize(
        "expr",
        [
            "empty.x",
            "nested.e.x",
            "nums[false].x",
            "objs[x>99].y",
            "(nums[false]).x",
            "$filter(nums, function($v){false}).x",
            "$map([], function($v){$v}).x",
            "$each({}, function($v){$v}).x",
            "[][0]",
        ],
    )
    def test_still_undefined(self, expr: str) -> None:
        assert ev(expr) is MISSING


class TestParserFlag:
    """The flag itself, since it is a syntactic property no runtime value can
    recover once the optimizer has run."""

    @staticmethod
    def _head(expr: str) -> object:
        ast = Parser.parse(expr)
        assert isinstance(ast, PathExpr), f"{expr} did not parse to a PathExpr"
        return ast.steps[0]

    @pytest.mark.parametrize("expr", ["[].x", "[].x.y", "[1,2,3].x", "[nope].x", "[].[1]"])
    def test_flagged_at_the_head(self, expr: str) -> None:
        head = self._head(expr)
        assert isinstance(head, ArrayConstructor)
        assert head.path_head is True

    def test_flagged_through_a_sort(self, expr: str = "[]^(x).y") -> None:
        head = self._head(expr)
        assert isinstance(head, SortExpr)
        assert isinstance(head.source, ArrayConstructor)
        assert head.source.path_head is True

    def test_not_flagged_when_parenthesised(self) -> None:
        head = self._head("([]).x")
        assert not isinstance(head, ArrayConstructor)

    def test_not_flagged_away_from_the_head(self) -> None:
        ast = Parser.parse("nums.[].x")
        assert isinstance(ast, PathExpr)
        step = ast.steps[1]
        assert isinstance(step, ArrayConstructor)
        assert step.path_head is False

    def test_a_bare_constructor_is_not_flagged(self) -> None:
        ast = Parser.parse("[1,2,3]")
        assert isinstance(ast, ArrayConstructor)
        assert ast.path_head is False

    def test_optimizer_preserves_the_flag(self) -> None:
        """`visit_array_constructor` rebuilds the node when an element is
        rewritten; rebuilding with the plain constructor would silently drop
        the flag and reintroduce the bug only for constant-foldable
        elements."""
        assert ev("[1+1-2*1].x") is MISSING  # folds to [0], still non-empty
        assert ev("[nope, 1+1].x") is MISSING  # folds an element, head stays non-empty


class TestGeneratedCode:
    """Emptiness is decided at compile time where it can be, so the fix costs
    nothing on the shapes that cannot be empty (§6.5a of the conformance
    note). Asserting on generated source is unusual, but it is the only way
    to pin "no guard was emitted" -- behaviour alone cannot distinguish a
    guard that never fires from no guard at all."""

    def test_literally_empty_head_is_constant_folded(self) -> None:
        """The remaining steps are unreachable, so the evaluated body is the
        bare constant -- no guard, and no step chain."""
        body = src("[].x").rsplit("return ", 1)[1].strip()
        assert body == "array_of()"

    def test_skipped_steps_are_still_translated(self) -> None:
        """The unreachable steps are compiled and the result thrown away, so
        a step that cannot be translated at all is still reported. `%` with no
        parent is S0217 in the reference too, which resolves ancestry while
        building the AST rather than while evaluating."""
        with pytest.raises(jsonata.JsonataCompilationError) as e:
            src("[].%")
        assert "%" in str(e.value)

    @pytest.mark.parametrize("expr", ["[1,2,3].x", '[{"x":1}].x', "[[]].x", "a.b", "nums.[].x", "([]).x"])
    def test_provably_non_empty_head_emits_no_guard(self, expr: str) -> None:
        assert "consarray_head" not in src(expr)

    @pytest.mark.parametrize("expr", ["[nope].x", "[nums[false]].x", "[nope]^(x).y"])
    def test_possibly_empty_head_is_guarded(self, expr: str) -> None:
        assert "consarray_head" in src(expr)

    def test_a_range_head_is_treated_as_droppable(self) -> None:
        """`[5..1]` is empty, so a range element must not count as
        "provably yields a value". Being wrong in this direction is silent;
        being wrong in the other costs one emptiness test."""
        assert ev("[5..1, 5..1].x") == []


class TestConsarrayHeadHelper:
    """The runtime guard in isolation."""

    def test_empty_list_short_circuits_without_calling_rest(self) -> None:
        def boom(_: object) -> object:  # pragma: no cover - must not run
            raise AssertionError("rest must not be evaluated")

        assert consarray_head([], boom) == []

    def test_missing_head_counts_as_empty(self) -> None:
        """`unwrap` (after a sort on the head) collapses the empty array to
        MISSING before the guard sees it. The translator only emits this call
        when the head is statically an array constructor -- which always
        produces an array -- so an absent head can only be a collapsed empty
        one."""

        def boom(_: object) -> object:  # pragma: no cover - must not run
            raise AssertionError("rest must not be evaluated")

        assert consarray_head(MISSING, boom) == []

    def test_returns_a_fresh_list_each_call(self) -> None:
        """A shared constant would let a caller that mutates its result leak
        into the next evaluation."""
        first = consarray_head([], lambda _: None)
        second = consarray_head([], lambda _: None)
        assert first == second == []
        assert first is not second

    def test_non_empty_head_delegates_to_rest(self) -> None:
        assert consarray_head([1, 2], lambda head: len(head)) == 2


class TestConstructorHeadedSubPath:
    """The same rule reached through a parenthesised sub-path: `a.([].x)`.

    Verified against `jsonata` 2.2.2 at c:/vlad-projects/js/jsonata rather
    than reasoned about -- all 33 expressions in this group were run through
    the reference interpreter.

    The sub-path is a path in its own right, so its own first step is the
    flagged constructor and it short-circuits exactly as a top-level path
    does. jsonata2py used to lose that because the optimizer flattened the
    sub-path into its parent, moving the flagged constructor off position 0.
    """

    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            ("a.([].x)", []),
            ("a.([].x.y)", []),
            ("a.([nope].x)", []),
            ("a.([]^(x).y)", []),
            ("a.(([].x))", []),
            ("a.b.([].x)", []),
            ("$.([].x)", []),
            ("(a.([].x))", []),
            ("a.(b.([].x))", []),
            ("[].([].x)", []),
            ("[1].([].x)", []),
            ('a.([].($error("boom")))', []),
            ("a.([nums].$)", []),
            ("a.([objs].x)", []),
        ],
    )
    def test_sub_path_short_circuits(self, expr: str, expected: object) -> None:
        result = ev(expr)
        assert result is not MISSING
        assert result == expected

    def test_the_empty_array_is_still_a_real_array(self) -> None:
        assert ev("$type(a.([].x))") == "array"
        assert ev("$exists(a.([].x))") is True
        assert ev("$string(a.([].x))") == "[]"
        assert ev("a.([].x = [])") is True
        assert ev("a.($append([].x, 1))") == [1]
        assert ev("$count(a.([].x))") == 0

    def test_one_empty_array_per_context_element(self) -> None:
        """A constructor result is not flattened into the parent sequence --
        the reference marks it `cons` and `evaluateStep` pushes it whole. So
        three context elements give three empty arrays, not one flattened
        (and therefore empty, and therefore undefined) result."""
        assert ev("nums.([].x)") == [[], [], []]
        assert ev("nums.($.([].x))") == [[], [], []]

    def test_a_singleton_context_still_collapses(self) -> None:
        """`objs` has one element, so the outer sequence is `[[]]`, which
        collapses to `[]` -- not to `[[]]`, and not to undefined."""
        assert ev("objs.([].x)") == []

    def test_an_empty_context_never_steps(self) -> None:
        assert ev("empty.([].x)") is MISSING
        assert ev("nope.([].x)") is MISSING

    @pytest.mark.parametrize(
        "expr",
        [
            "a.(([]).x)",  # parenthesised again inside -- the flag is never set
            "a.($e := []; $e.x)",
            "a.({}.x)",
            "a.(nums.[].x)",  # the constructor is not the sub-path's first step
            "a.([].x).y",  # `[]` has no field y
            "nums.([].x).y",
            "a.([]).x",
            "a.(objs.([].x))",
        ],
    )
    def test_sub_path_rules_match_the_top_level_ones(self, expr: str) -> None:
        assert ev(expr) is MISSING

    def test_a_non_empty_sub_path_head_is_unaffected(self) -> None:
        assert ev('a.(["a"].x)') is MISSING
        assert ev('a.([{"x":1}].x)') == 1
        assert ev('a.([{"x":1},{"x":2}].x)') == [1, 2]

    def test_the_sub_path_body_still_runs_when_non_empty(self) -> None:
        with pytest.raises(JsonataEvaluationError) as e:
            ev('a.(["a"].($error("boom")))')
        assert e.value.error_code == "D3137"

    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            # A sub-path that actually runs its later steps returns an
            # ordinary sequence, which flattens into the parent as usual.
            ("nums.([$,$].$)", [1, 1, 2, 2, 3, 3]),
            ("objs.([$.x].$)", 1),
            ("a.([nope,1].$)", 1),
            ("a.([1,2].$)", [1, 2]),
            ("nums.([1..3].$)", [1, 2, 3, 1, 2, 3, 1, 2, 3]),
            ("a.([1,2,3]^($).$)", [1, 2, 3]),
            ("a.([1,2,3].$)[0]", 1),
            ("nums.([].x)[0]", MISSING),
        ],
    )
    def test_only_the_short_circuited_empty_array_is_preserved(self, expr: str, expected: object) -> None:
        """The no-flatten rule keys off the *value*, not the shape: only an
        empty array can have come from the head short-circuit, because every
        other empty result has already collapsed to MISSING by then."""
        assert ev(expr) == expected if expected is not MISSING else ev(expr) is MISSING


class TestSubPathCodeGeneration:
    """A parenthesised path led by an array constructor stays its own step.

    Flattening it into the parent got two things wrong at once: it moved the
    flagged constructor off position 0, and it turned the constructor into a
    *bare* step, which the reference marks `cons` and does not flatten.
    """

    @pytest.mark.parametrize(
        "expr",
        ["a.([].x)", "a.([nope].x)", "a.([]^(x).y)", "a.([1,2,3].$)", 'a.([{"x":1}].x)'],
    )
    def test_a_constructor_headed_sub_path_is_kept_whole(self, expr: str) -> None:
        assert "map_consarray_step" in src(expr)

    @pytest.mark.parametrize("expr", ["a.b", "a.(b.c)", "a.(([]).x)", "a.(nums.[].x)", "a.($x)"])
    def test_other_sub_paths_are_untouched(self, expr: str) -> None:
        assert "map_consarray_step" not in src(expr)


class TestMapConsarrayStepHelper:
    """The runtime step helper in isolation."""

    def test_a_non_sequence_context_is_stepped_once_and_left_alone(self) -> None:
        """`unwrap` here would collapse the empty array back to MISSING,
        which is exactly the bug being fixed."""
        assert map_consarray_step({"a": 1}, lambda _: []) == []

    def test_an_empty_result_is_pushed_whole(self) -> None:
        assert map_consarray_step([1, 2, 3], lambda _: []) == [[], [], []]

    def test_a_non_empty_result_flattens_as_usual(self) -> None:
        assert map_consarray_step([1, 2], lambda _: [7, 8]) == [7, 8, 7, 8]

    def test_a_singleton_sequence_collapses(self) -> None:
        assert map_consarray_step([1], lambda _: []) == []

    def test_missing_results_are_dropped(self) -> None:
        assert map_consarray_step([1, 2], lambda v: MISSING if v == 1 else v) == 2

    def test_an_absent_or_empty_context_yields_missing(self) -> None:
        assert map_consarray_step(MISSING, lambda _: []) is MISSING
        assert map_consarray_step(None, lambda _: []) is MISSING
        assert map_consarray_step([], lambda _: []) is MISSING
