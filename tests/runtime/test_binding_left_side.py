"""What `@$v` and `#$v` attach to (§19 of the cross-port notes).

One rule explains almost all of it: **only the `.` production makes a path.**
`@` hangs the focus off whatever the left side already was, so when that is not
path-shaped there is no path, nothing to revert to, and no path step -- which
is why `1@$e` is `1` rather than S0213, and `a ~> $string() @$e` binds the
stringified `a` rather than the bare `$string`.

Expectations come from the reference at `c:/vlad-projects/js/jsonata` (2.2.2).
On the 960-row `@`/`#` corpus these rules take this port from 254 divergences
to 9; jsonata2js, whose tuple model this one is ported from, has 183.
"""

from __future__ import annotations

import pytest

import jsonata2py as jsonata
from jsonata2py.runtime.values import MISSING

DOC = {"a": {"b": 1, "c": [2, 3]}, "nums": [1, 2, 3], "objs": [{"x": 1}, {"x": 2}]}


def ev(expr: str, data: object = DOC) -> object:
    return jsonata.JsonataExpressionFactory().compile(expr).evaluate(data)


class TestANonPathLeftSideMakesNoPath:
    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            # The value passes through: no path, so no revert.
            ("1@$e", 1),
            ("true@$e", True),
            ('"z"@$e', "z"),
            ("{}@$e", {}),
            ("[1]@$e", [1]),
            ("$sum(nums)@$e", 6),
            ("(a.b)@$e", 1),
            ("*@$e", [{"b": 1, "c": [2, 3]}, 1, 2, 3, {"x": 1}, {"x": 2}]),
            # ...and a *stage* after it is still not a path step.
            ("1@$e[0]", 1),
            ("1@$e[]", 1),
            ("$sum(nums)@$e[]", 6),
            ("(nums)@$e[0]", 1),
            ("(a.b)[]@$e", 1),
            # A path-shaped left side *does* revert.
            ("a.b@$e", {"b": 1, "c": [2, 3]}),
            ("nums@$e.$e", [1, 2, 3]),
        ],
    )
    def test_the_left_side_decides(self, expr: str, expected: object) -> None:
        assert ev(expr) == expected

    def test_a_dot_after_the_binding_does_make_a_path(self) -> None:
        """And then the ordinary path rules apply -- including S0213 for a
        literal step, and the quoted-name rule for a string one."""
        assert ev("$sum(nums)@$e.$") == DOC
        assert ev('"z"@$e.$') is MISSING  # reads field `z`
        with pytest.raises(jsonata.JsonataCompilationError) as exc:
            ev("1@$e.$e")
        assert exc.value.error_code == "S0213"


class TestBindingsBindLooserThanChaining:
    """`a ~> $string() @$e` binds the *stringified* `a`. Parsing the binding
    inside the postfix level attached it to `$string()` alone, which is a bare
    function where a value belonged -- T2006."""

    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            ("a ~> $string() @$e", '{"b":1,"c":[2,3]}'),
            ("nums ~> $sum() @$e", 6),
            ("a ~> $string() #$i", '{"b":1,"c":[2,3]}'),
            # A binding on the chain's *first* operand still binds there.
            ("a@$e ~> $string()", '{"a":{"b":1,"c":[2,3]},"nums":[1,2,3],"objs":[{"x":1},{"x":2}]}'),
        ],
    )
    def test_chaining(self, expr: str, expected: object) -> None:
        assert ev(expr) == expected


class TestABindingPushesInsideAGroupBy:
    """`@` sets the focus on the last *step*, and evaluatePath runs the group
    last -- over the tuple stream the binding produced. So the group sees the
    reverted context."""

    def test_the_group_sees_the_reverted_context(self) -> None:
        assert ev('a{"k":$string($)}@$e') == {"k": jsonata.compile("$string($)").evaluate(DOC)}
        assert ev('a.b{"k":$string($)}@$e') == {"k": '{"b":1,"c":[2,3]}'}

    def test_a_group_without_a_binding_is_unchanged(self) -> None:
        assert ev('a{"k":b}') == {"k": 1}


class TestAConstructorHeadWithABinding:
    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            # A focus on the head means the head never advances the stream:
            # its value is computed and discarded.
            ("[]@$e.$", DOC),
            # The empty array is the result only when a real step follows.
            ("[].x@$e", []),
            ('[].{"k":$}@$e', []),
            ("[]#$i", MISSING),
            ("[]#$i[]", MISSING),
        ],
    )
    def test_constructor_heads(self, expr: str, expected: object) -> None:
        assert ev(expr) == expected


class TestS0215ReachesThroughWhatSitsBetween:
    """A binding cannot follow a predicate step, and a `[]`, a group-by or an
    earlier binding in between does not hide one."""

    @pytest.mark.parametrize(
        "expr",
        ["nums[0]@$e", "nums[0][]@$e", 'nums[0]{"k":1}@$e', "nums[0]#$i@$e"],
    )
    def test_s0215(self, expr: str) -> None:
        with pytest.raises(jsonata.JsonataCompilationError) as exc:
            ev(expr)
        assert exc.value.error_code == "S0215"

    def test_a_step_after_the_predicate_clears_it(self) -> None:
        assert ev("nums[0].x@$e") is MISSING


class TestAConstructorHeadAndTheTupleStream:
    """§20: measured with a cross-product over left side, binding form and tail,
    not derived. Three rules, each one line of `evaluatePath`."""

    def test_consarray_is_set_by_the_dot_production_alone(self) -> None:
        """The decisive pair. Without a `.` there is no consarray flag, so the
        constructor is an ordinary tuple step; with one, the head
        short-circuits as a value and the stream restarts from the input."""
        assert ev("[1,2]#$i@$e") == [DOC, DOC]
        assert ev("[1,2]#$i@$e.$") == DOC

    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            # `@` on the head stops it advancing the stream...
            ("[]@$e.$", DOC),
            ("[1]#$i@$e.$", DOC),
            # ...but `#` alone does not.
            ("[1]#$i.$", 1),
            ("[1,2]#$i.$", [1, 2]),
            ("[a.b]#$i.$", 1),
        ],
    )
    def test_only_a_focus_stops_the_head(self, expr: str, expected: object) -> None:
        assert ev(expr) == expected

    @pytest.mark.xfail(reason="§20.4: the last open row -- see the mark question", strict=True)
    def test_an_empty_constructor_head_under_a_position_binding(self) -> None:
        """`[]#$i.$` is undefined in the reference and `[]` here. It is the
        same unresolved question as `a.b#$i[]`: whether a path that carried a
        `#$v` is still a sequence for the collapse to act on."""
        assert ev("[]#$i.$") is MISSING

    @pytest.mark.parametrize(
        "expr",
        ["[1]#$i.$i", "[1]@$e.$e", "[nums]#$i@$e.$e", "[a.b]@$e#$i.$i"],
    )
    def test_a_constructor_heads_bindings_never_reach_the_rest(self, expr: str) -> None:
        """The consarray branch evaluates the head outside the tuple
        machinery, so no bindings are ever made -- while the same shapes over
        a field head resolve normally."""
        assert ev(expr) is MISSING
        assert ev("nums#$i.$i") == [0, 1, 2]

    @pytest.mark.parametrize(
        ("expr", "expected"),
        [("[1]#$i@$e", DOC), ("[a.b]#$i@$e", DOC), ("[1,2]#$i@$e", [DOC, DOC])],
    )
    def test_a_binding_reverts_to_the_paths_input(self, expr: str, expected: object) -> None:
        assert ev(expr) == expected

    @pytest.mark.parametrize("expr", ["[1,2]@$e^($)", "[nums]@$e^($)"])
    def test_sorting_the_reverted_documents_is_t2008(self, expr: str) -> None:
        """Which is itself the evidence that the revert happened."""
        with pytest.raises(jsonata.JsonataEvaluationError) as exc:
            ev(expr)
        assert exc.value.error_code == "T2008"
