"""One pass over a bound sequence instead of many.

`translator/scan_fusion.py` groups the operations a block performs over one
bound sequence and emits a single helper that reads each distinct field once
per element. On the project's benchmark that is 551 field reads per
evaluation down to 320, and +36% throughput.

Two things need pinning, and they pull in opposite directions:

* **that it fires** -- an optimisation nobody notices has silently stopped
  working is the normal way these decay, and every correctness test here would
  still pass with the pass disabled. `TestWhatFuses` asserts on generated
  source for that reason;
* **that it declines** -- the whitelist of unconditionally-evaluated positions
  is what stops an operation being hoisted out of a branch the expression
  never takes. Failing open there means wrong answers, not lost speed, so
  `TestWhatDeclines` is the more important half.

The behavioural tests all take the shape "fused answer == un-fused answer",
because that is the actual contract: the pass may change nothing but the time.
Data that defeats the fast arm is deliberately over-represented -- that is
where the RESCAN fallback lives, and it is the part real documents never
exercise.
"""

from __future__ import annotations

import pytest

import jsonata2py as jsonata
from jsonata2py.runtime.values import MISSING

_FACTORY = jsonata.JsonataExpressionFactory()


def src(expr: str) -> str:
    return _FACTORY.translate(expr)


def ev(expr: str, data: object) -> object:
    return _FACTORY.compile(expr).evaluate(data)


def outcome(expr: str, data: object) -> object:
    """The value, or the error code -- both are part of what must not change."""
    try:
        return ("ok", ev(expr, data))
    except jsonata.JsonataError as e:
        return ("err", getattr(e, "error_code", None))


def fuses(expr: str) -> bool:
    return "def _scan" in src(expr)


#: Documents chosen to walk every arm: the fast one, each way of missing it,
#: and the shapes that are not sequences of objects at all.
DATA_SHAPES = [
    pytest.param([{"f": 1, "g": "a", "h": True, "k": 2}, {"f": 2.5, "g": "b", "h": False, "k": 2}], id="plain"),
    pytest.param([{"f": "text", "g": "a"}, {"f": 2, "g": "b"}], id="non-numeric-field"),
    pytest.param([{"f": [1, 2], "g": "a"}, {"f": 3, "g": "b"}], id="list-field"),
    pytest.param([{"f": True, "g": "a"}, {"f": 1, "g": "b"}], id="bool-field"),
    pytest.param([{"f": None, "g": "a"}, {"f": 1, "g": "b"}], id="null-field"),
    pytest.param([{"g": "a"}, {"f": 1, "g": "b"}], id="absent-field"),
    pytest.param([{"f": 1}, "scalar", 7, None], id="non-object-elements"),
    pytest.param([{"f": 12345678901234567890, "g": "a"}], id="beyond-2**53"),
    pytest.param([{"f": 1, "g": 1}, {"f": 2, "g": "1"}], id="mixed-field-types"),
    pytest.param({"f": 1, "g": "a"}, id="single-object"),
    pytest.param([], id="empty"),
    pytest.param(None, id="null-input"),
    pytest.param(5, id="scalar-input"),
]


class TestWhatFuses:
    """The pass has to actually fire, or every other test here passes with it
    turned off."""

    @pytest.mark.parametrize(
        ("expr", "why"),
        [
            ("($s := d; [$sum($s.f), $max($s.f), $min($s.f)])", "three aggregates"),
            ("($s := d; [$sum($s.f), $max($s.f)])", "two sharing one field"),
            ("($s := d; [$sum($s.f), $max($s.g), $min($s.h)])", "three, no shared field"),
            ("($s := d; [$count($s[g='a']), $count($s[g='b']), $count($s[g='c'])])", "counts"),
            ("($s := d; [$s[g='a'], $s[g='b'], $s[g='c']])", "filters used as values"),
            ("($s := d; [$round($average($s.f), 1), $sum($s.f), $max($s.f)])", "nested in another call"),
            ("($s := d; [$sum($s.f), $max($s.f), $min($s.f)] and $x)", "left of `and`"),
            ("($s := d; ([$sum($s.f), $max($s.f), $min($s.f)] = 0) ? 1 : 2)", "a conditional's condition"),
            ("($s := d; {'a': $sum($s.f), 'b': $max($s.f), 'c': $min($s.f)})", "object constructor values"),
        ],
    )
    def test_fires(self, expr: str, why: str) -> None:
        assert fuses(expr), f"expected fusion ({why}) in:\n{src(expr)}"

    def test_one_helper_per_sequence(self) -> None:
        """Grouping is by sequence, so two bound names are two passes."""
        text = src("($s := d; $t := e; [$sum($s.f), $max($s.f), $min($s.f), $sum($t.f), $max($t.f), $min($t.f)])")
        assert text.count("def _scan") == 2

    def test_each_distinct_field_is_read_once_per_element(self) -> None:
        """The point of the whole pass. Four operations on `f` and two on `g`
        become one `.get` each."""
        text = src("($s := d; [$sum($s.f), $average($s.f), $max($s.f), $min($s.f), $count($s[g='a']), $s[g='b']])")
        scan = text[text.index("def _scan") :]
        assert scan.count(".get('f'") == 1
        assert scan.count(".get('g'") == 1


def _accumulators(expr: str) -> list[str]:
    """The scan helper's accumulator initialisations, one per absorbed op.

    Only the lines above the loop -- the folds inside it also start with the
    accumulator prefix, and there are several of those per operation.
    """
    text = src(expr)
    scan = text[text.index("def _scan") : text.index("def _block0")]
    header = scan[: scan.index("    for ")]
    return [line.strip() for line in header.splitlines() if line.strip().startswith("_a")]


class TestAMatchedNodeIsAbsorbedWhole:
    """A matched operation must not also be descended into.

    `$count($s[g = 'a'])` contains a filter that would match on its own, so a
    walk that recurses into an already-absorbed node collects it twice: once
    as the count, once as a filter. The counts stayed correct, so nothing
    behavioural could see it -- the scan just also built a list of matching
    elements that no slot ever read, on every element of every evaluation.

    Hence assertions on accumulator *count*: one per absorbed operation and
    not one more. This is the shape of bug an optimisation pass hides best.
    """

    def test_a_count_does_not_also_collect_its_own_filter(self) -> None:
        accumulators = _accumulators("($s := d; [$count($s[g='a']), $count($s[g='b']), $count($s[g='c'])])")
        assert accumulators == ["_a1_0c = 0", "_a1_1c = 0", "_a1_2c = 0"]

    @pytest.mark.parametrize(
        ("expr", "ops"),
        [
            ("($s := d; [$count($s[g='a']), $count($s[g='b']), $count($s[g='c'])])", 3),
            ("($s := d; [$s[g='a'], $s[g='b'], $s[g='c']])", 3),
            ("($s := d; [$count($s[g='a']), $s[g='a'], $count($s[g='b'])])", 3),
            ("($s := d; [$sum($s.f), $max($s.f), $count($s[g='a']), $s[g='b']])", 4),
            ("($s := d; [$round($count($s[g='a'])), $count($s[g='b']), $count($s[g='c'])])", 3),
            # The outer filter cannot match (its source is not a bound name),
            # so only the inner one is absorbed -- once.
            ("($s := d; [$count($s[g='a'][h='b']), $count($s[g='b']), $count($s[g='c'])])", 3),
            ("($s := d; [$s[g='a'][h='b'], $s[g='b'], $s[g='c']])", 3),
        ],
    )
    def test_one_accumulator_per_absorbed_operation(self, expr: str, ops: int) -> None:
        assert len(_accumulators(expr)) == ops, src(expr)

    def test_a_predicate_body_is_never_an_unconditional_position(self) -> None:
        """`$t[x=1]` inside another filter's predicate runs per element, so it
        is not the enclosing block's to hoist -- only the two literal counts
        are absorbed."""
        expr = "($s := d; [$count($s[g = $t[x=1].y]), $count($s[g='b']), $count($s[g='c'])])"
        assert len(_accumulators(expr)) == 2


class TestWhatDeclines:
    """A whitelist, not a blacklist. Every row is a way of being wrong that
    costs correctness rather than speed."""

    @pytest.mark.parametrize(
        ("expr", "why"),
        [
            ("($s := d; $s := e; [$sum($s.f), $max($s.f), $min($s.f)])", "sequence rebound in the block"),
            ("([$sum($s.f), $max($s.f), $min($s.f)]; $s := d)", "sequence bound after its use"),
            ("($s := d; $x ? [$sum($s.f), $max($s.f), $min($s.f)] : 0)", "a conditional branch"),
            ("($s := d; $x or [$sum($s.f), $max($s.f), $min($s.f)] = 0)", "right of `or`"),
            ("($s := d; $x and [$sum($s.f), $max($s.f), $min($s.f)] = 0)", "right of `and`"),
            ("($s := d; $f := function(){[$sum($s.f), $max($s.f), $min($s.f)]}; $f())", "inside a lambda"),
            ("[$sum(d.f), $max(d.f), $min(d.f)]", "not a bound name at all"),
            ("($s := d; [$sum($s.a.f), $max($s.a.f), $min($s.a.f)])", "two-level path"),
            ("($s := d; [$sum($s.f), $max($s.g)])", "two operations, no shared field"),
            ("($s := d; [$s.sum(1), $s.max(1), $s.min(1)])", "bare name() path-step calls"),
            ("($s := d; [$count($s[f>1]), $count($s[f>2]), $count($s[f>3])])", "non-equality predicates"),
        ],
    )
    def test_declines(self, expr: str, why: str) -> None:
        assert not fuses(expr), f"expected NO fusion ({why}) in:\n{src(expr)}"

    def test_a_shadowed_builtin_declines_only_itself(self) -> None:
        """`$sum := function(...)` is no longer the built-in, so that call
        must compile as a user function -- while `$max` beside it still
        fuses."""
        text = src("($s := d; $sum := function($x){9}; [$sum($s.f), $max($s.f), $min($s.f)])")
        assert "fn_sum_field" not in text
        assert "fn_apply(v_sum" in text
        assert "def _scan" in text

    def test_a_non_literal_predicate_declines_only_itself(self) -> None:
        text = src("($s := d; [$count($s[g=$x]), $count($s[g='b']), $count($s[g='c'])])")
        assert "fn_count_filter" in text  # the dynamic one keeps its callback
        assert "def _scan" in text  # its two literal siblings still fuse

    def test_a_nested_block_plans_its_own(self) -> None:
        """A name the inner block does not own is not its to hoist."""
        text = src("($s := d; ($sum($s.f); $max($s.f); $min($s.f)))")
        assert not fuses("($s := d; ($sum($s.f); $max($s.f); $min($s.f)))"), text


class TestFusedEqualsUnfused:
    """The contract: the pass changes the time and nothing else.

    Each case is run against a shape that fuses and the equivalent that does
    not, so the assertion is a real comparison rather than a restated
    expectation.
    """

    @pytest.mark.parametrize("data", DATA_SHAPES)
    @pytest.mark.parametrize(
        ("fused", "plain"),
        [
            (
                "($s := d; [$sum($s.f), $average($s.f), $max($s.f), $min($s.f)])",
                "[$sum(d.f), $average(d.f), $max(d.f), $min(d.f)]",
            ),
            (
                "($s := d; [$count($s[g='a']), $count($s[g='b']), $count($s[h=true]), $count($s[k=2])])",
                "[$count(d[g='a']), $count(d[g='b']), $count(d[h=true]), $count(d[k=2])]",
            ),
            (
                "($s := d; [$s[g='a'], $s[g='b'], $s[h=true], $s[k=2]])",
                "[d[g='a'], d[g='b'], d[h=true], d[k=2]]",
            ),
            (
                "($s := d; [$sum($s.f), $s[g='a'], $count($s[g='b'])])",
                "[$sum(d.f), d[g='a'], $count(d[g='b'])]",
            ),
        ],
    )
    def test_same_answer(self, fused: str, plain: str, data: object) -> None:
        assert fuses(fused)
        assert not fuses(plain)
        assert outcome(fused, {"d": data}) == outcome(plain, {"d": data})

    @pytest.mark.parametrize("data", DATA_SHAPES)
    def test_same_error(self, data: object) -> None:
        """An aggregate that fails must fail the same way. The fused loop
        never raises: it flags the slot and the use site redoes the original
        call, so which error wins is not this pass's problem."""

        assert outcome("($s := d; [$sum($s.f), $max($s.f), $min($s.f)])", {"d": data}) == outcome(
            "[$sum(d.f), $max(d.f), $min(d.f)]", {"d": data}
        )

    def test_error_ordering_is_the_source_order(self) -> None:
        """`$sum` is bound first, so its error wins even though `$max` meets
        the offending element first."""
        data = {"d": [{"f": 1, "g": "x"}, {"f": "bad", "g": "y"}]}
        both = "($s := d; [$sum($s.f), $max($s.f), $min($s.f)])"
        with pytest.raises(jsonata.JsonataError) as fused_err:
            ev(both, data)
        with pytest.raises(jsonata.JsonataError) as plain_err:
            ev("[$sum(d.f), $max(d.f), $min(d.f)]", data)
        assert fused_err.value.error_code == plain_err.value.error_code

    def test_a_later_statement_still_pre_empts_a_hoisted_operation(self) -> None:
        """The scan is hoisted to the group's first use, but it does not
        raise: a `$error` between the first use and a failing aggregate still
        wins, because the aggregate only raises where it was written."""
        data = {"d": [{"f": "bad"}]}
        expr = "($s := d; $a := $count($s[f='bad']); $b := $error('boom'); [$a, $b, $sum($s.f), $max($s.f)])"
        with pytest.raises(jsonata.JsonataError) as e:
            ev(expr, data)
        assert e.value.error_code == "D3137"

    def test_the_filter_result_shape_is_preserved(self) -> None:
        """Nothing for no matches, the element itself for one, an array for
        several -- collecting into a list unconditionally would be both wrong
        and slower than not fusing."""
        data = {"d": [{"g": "a", "n": 1}, {"g": "b", "n": 2}, {"g": "b", "n": 3}]}
        expr = "($s := d; [$s[g='a'], $s[g='b'], $s[g='zz']])"
        assert fuses(expr)
        assert ev("($s := d; $s[g='a'])", data) == {"g": "a", "n": 1}
        assert ev("($s := d; [$s[g='a'], $s[g='b'], $s[g='c']])", data) == ev("[d[g='a'], d[g='b'], d[g='c']]", data)
        assert ev("($s := d; [$s[g='zz'], $s[g='a'], $s[g='b']])", data) == ev("[d[g='zz'], d[g='a'], d[g='b']]", data)

    def test_repeated_evaluation_is_stable(self) -> None:
        """The scan helper is module-level and shared by every evaluate();
        an accumulator left at module scope would give a different answer the
        second time."""
        compiled = _FACTORY.compile("($s := d; [$sum($s.f), $max($s.f), $count($s[g='a']), $s[g='a']])")
        data = {"d": [{"f": 1, "g": "a"}, {"f": 2, "g": "b"}]}
        first = compiled.evaluate(data)
        assert compiled.evaluate(data) == first
        assert compiled.evaluate(data) == first


class TestRescanFallback:
    """The fast arm covers a field holding a plain int or float. Everything
    else falls back to the original call, at its original position."""

    def test_the_fallback_is_the_original_helper(self) -> None:
        text = src("($s := d; [$sum($s.f), $max($s.f), $min($s.f)])")
        assert "if _sv1[0] is not RESCAN else fn_sum_field(v_s, 'f')" in text
        assert "fn_max_field(v_s, 'f')" in text

    def test_a_filter_falls_back_to_the_monomorphized_form(self) -> None:
        text = src("($s := d; [$s[g='a'], $s[g='b'], $s[g='c']])")
        assert "filter_field_eq(v_s, 'g', 'a')" in text

    def test_counting_needs_no_fallback(self) -> None:
        """fn_count_field_eq is total, and its per-kind comparison is
        reproduced in full, so a count slot is read bare."""
        text = src("($s := d; [$count($s[g='a']), $count($s[g='b']), $count($s[g='c'])])")
        assert "RESCAN" not in text

    @pytest.mark.parametrize(
        "value", [[1, 2], "text", True, None, {"x": 1}], ids=["list", "str", "bool", "null", "object"]
    )
    def test_a_value_outside_the_fast_arm_still_agrees(self, value: object) -> None:
        data = {"d": [{"f": value}, {"f": 3}]}
        assert outcome("($s := d; [$sum($s.f), $max($s.f), $min($s.f)])", data) == outcome(
            "[$sum(d.f), $max(d.f), $min(d.f)]", data
        )


class TestMonomorphizedFilter:
    """`$seq[field = <literal>]` compiles to filter_field_eq whether or not
    anything fuses -- which is what lets a fused slot name its own fallback."""

    @pytest.mark.parametrize(
        ("expr", "data", "expected"),
        [
            ("d[g='a']", [{"g": "a"}, {"g": "b"}], {"g": "a"}),
            ("d[g='zz']", [{"g": "a"}], MISSING),
            ("d[n=2]", [{"n": 1}, {"n": 2}, {"n": 2}], [{"n": 2}, {"n": 2}]),
            ("d[h=true]", [{"h": True}, {"h": False}], {"h": True}),
            ("d[h=false]", [{"h": True}, {"h": 0}], MISSING),
            ("d[n=1]", {"n": 1}, {"n": 1}),
            ("d[n=1]", [], MISSING),
            ("d[g='a']", [{"g": ["a"]}], MISSING),
        ],
    )
    def test_matches_the_callback_form(self, expr: str, data: object, expected: object) -> None:
        got = ev(expr, {"d": data})
        if expected is MISSING:
            assert got is MISSING
        else:
            assert got == expected

    def test_bool_and_number_do_not_cross_match(self) -> None:
        """JSONata `=` compares kind before value, so `true` is not `1`."""
        assert ev("d[h=true]", {"d": [{"h": 1}]}) is MISSING
        assert ev("d[n=1]", {"d": [{"n": True}]}) is MISSING
