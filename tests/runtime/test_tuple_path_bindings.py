"""`@$v` and `#$v` through the tuple stream (§14.3 / §15 of the cross-port notes).

The model is ported from jsonata2js's `src/runtime/path.js`, which is the only
one of the three ports that had it. What it buys, and what these pin:

* **`@$v` binds without navigating.** It records the step's result under the
  name and reverts the stream's value to that step's *input*, so `nums@$e` is
  the document once per element rather than the elements.
* **A `%` walks the parent chain** carried on the tuple.
* **A stage indexes within a sibling group**, and switches to indexing across
  the whole stream once the path is in tuple mode -- which is the difference
  between `$#$pos` and `$.$#$pos`.

Expectations come from the reference at `c:/vlad-projects/js/jsonata` (2.2.2).
Where a sibling port disagrees it is noted, because on this family the ports
disagree with each other in both directions and the reference is the only
arbiter.
"""

from __future__ import annotations

import pytest

import jsonata2py as jsonata
from jsonata2py.runtime.values import MISSING

DOC = {
    "a": {"b": 1, "c": [2, 3]},
    "nums": [1, 2, 3],
    "objs": [{"x": 1}, {"x": 2}],
    "one": [7],
    "empty": [],
}

LIBRARY = {
    "library": {
        "loans": [
            {"customer": "10001", "isbn": "9780262510871"},
            {"customer": "10003", "isbn": "9780201530827"},
        ],
        "books": [
            {"isbn": "9780262510871", "title": "SICP"},
            {"isbn": "9780201530827", "title": "AoCP"},
        ],
    }
}


def ev(expr: str, data: object = DOC) -> object:
    return jsonata.JsonataExpressionFactory().compile(expr).evaluate(data)


class TestAContextBindDoesNotNavigate:
    """The one that made the port worth doing."""

    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            # The stream reverts to the step's input, once per result.
            ("nums@$e", [DOC, DOC, DOC]),
            ("a@$e", DOC),
            ("a.b@$e", {"b": 1, "c": [2, 3]}),
            ("a.b@$e.$", {"b": 1, "c": [2, 3]}),
            # ...while the binding itself holds what the step produced.
            ("nums@$e.$e", [1, 2, 3]),
            ("a.b@$e.$e", 1),
            # A field step after a bind resolves against the reverted context,
            # so it is *not* a field of the bound value.
            ("objs@$e.x", MISSING),
        ],
    )
    def test_the_context_reverts(self, expr: str, expected: object) -> None:
        assert ev(expr) == expected

    def test_the_documented_join_idiom(self) -> None:
        """`Contact` is not a field of an Employee: once a binding is active a
        field step that misses per-element resolves against the root."""
        got = ev(
            "library.loans@$l.books@$b[$l.isbn = $b.isbn].{'title': $b.title, 'who': $l.customer}",
            LIBRARY,
        )
        assert got == [
            {"title": "SICP", "who": "10001"},
            {"title": "AoCP", "who": "10003"},
        ]


class TestAPositionBind:
    @pytest.mark.parametrize(
        ("expr", "expected"),
        [("nums#$i.$i", [0, 1, 2]), ("nums#$i[$i > 0]", [2, 3])],
    )
    def test_a_position_bind_records_the_index(self, expr: str, expected: object) -> None:
        assert ev(expr) == expected

    def test_a_leading_dollar_is_the_seed_not_a_step(self) -> None:
        """The discriminating pair. A leading `$` *is* the seed, so there is
        one outer item and the indices run 0..n-1 across it. A second `$` is a
        step, and a step re-parents: every element becomes its own outer item,
        so its index restarts at 0 and the filter keeps all of them."""
        assert ev("$#$pos[$pos<2]", [1, 2, 3]) == [1, 2]
        assert ev("$.$#$pos[$pos<2]", [1, 2, 3]) == [1, 2, 3]


class TestAStageOverATupleStream:
    def test_a_stage_indexes_across_the_whole_stream(self) -> None:
        """Once a path is in tuple mode the reference expands and flattens
        every outer tuple's result *before* running the stages, so `[0]` picks
        the first match overall rather than the first within each group."""
        assert ev("library.loans@$l.books@$b[$l.isbn = $b.isbn][0].$b.title", LIBRARY) == "SICP"

    def test_a_numeric_subscript_folds_onto_the_binding(self) -> None:
        """`nums#$i[0]` is one step -- the parser folds the subscript onto the
        binding -- which the value-mode compiler could not represent at all."""
        assert ev("nums#$i[0]") == 1
        assert ev("objs#$i[0]") == {"x": 1}
        assert ev("empty#$i[0]") is MISSING


class TestASortOverATupleStream:
    def test_sorting_by_a_binding_keeps_the_bindings(self) -> None:
        """Collapsing to values before the sort would drop them, and the key
        expression reads one."""
        assert ev("nums@$e^($e).$e") == [1, 2, 3]
        assert ev("nums@$e^(>$e).$e") == [3, 2, 1]

    def test_a_bad_sort_key_still_reports_t2008(self) -> None:
        with pytest.raises(jsonata.JsonataEvaluationError) as exc:
            ev("objs@$e^($e)")
        assert exc.value.error_code == "T2008"


class TestGroupByOverATupleStream:
    def test_the_pairs_see_the_bindings(self) -> None:
        assert ev("nums@$e{'k': $e}") == {"k": [1, 2, 3]}

    def test_colliding_buckets_append_their_bindings(self) -> None:
        """`reduceTupleStream`: when more than one tuple lands in a bucket its
        bindings append rather than the last one winning."""
        assert ev("nums#$i{'k': $i}") == {"k": [0, 1, 2]}


class TestValueModePathsAreUntouched:
    """Tuple mode is reached only for a path that carries a binding; nothing
    else allocates a tuple, which is why the benchmark did not move."""

    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            ("a.b", 1),
            ("nums", [1, 2, 3]),
            ("objs.x", [1, 2]),
            ("nums[0]", 1),
            ("a.c[1]", 3),
            ("one", [7]),
        ],
    )
    def test_ordinary_paths(self, expr: str, expected: object) -> None:
        assert ev(expr) == expected

    def test_a_binding_free_path_emits_no_tuple_calls(self) -> None:
        src = jsonata.JsonataExpressionFactory().translate("a.b.c[x > 1].d")
        assert "seed(" not in src
        assert "step_field(" not in src
