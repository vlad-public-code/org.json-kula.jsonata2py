"""Conformance cases found by differential testing against the reference.

Every expectation here was produced by running the expression through the
reference `jsonata` interpreter (and cross-checked against the sibling
jsonata2js port, which agreed with the reference on all 115 divergences a
1,001-case probe found). The official 1,281-file acceptance suite covers
none of these, which is why they regressed unnoticed.

Each test names the root cause it pins so a future refactor that reverts the
behaviour fails with an explanation rather than a bare value mismatch.
"""

from __future__ import annotations

import pytest

import jsonata2py as jsonata
from jsonata2py.errors import JsonataEvaluationError


def ev(expr: str, data: object = None) -> object:
    return jsonata.JsonataExpressionFactory().compile(expr).evaluate(data)


def code(expr: str, data: object = None) -> str | None:
    with pytest.raises(JsonataEvaluationError) as e:
        ev(expr, data)
    return e.value.error_code


class TestNumberRendering:
    """`$string` of a number is ECMA-262 Number::toString of the double,
    after non-integers are rounded to 15 significant digits. The previous
    hand-rolled notation rules diverged on subnormals, on the >=1e21
    exponential switch, and on integers beyond 2**53."""

    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            ("$string(1e-320)", "1e-320"),
            ("$string(-1e-320)", "-1e-320"),
            ("$string(5e-324)", "5e-324"),
            ("$string(1e21)", "1e+21"),
            ("$string(1e20)", "100000000000000000000"),
            ("$string(1e-6)", "0.000001"),
            ("$string(1e-7)", "1e-7"),
            ("$string(12345678901234567890)", "12345678901234567000"),
            ("$string($power(2,70))", "1.1805916207174113e+21"),
            ("$string(1e16)", "10000000000000000"),
            ("$string(0.1)", "0.1"),
            ("$string(1/3)", "0.333333333333333"),
        ],
    )
    def test_ecma_number_to_string(self, expr: str, expected: str) -> None:
        assert ev(expr) == expected

    def test_floor_ceil_stay_doubles(self) -> None:
        """math.floor returns an arbitrary-precision int, so $floor(1e21)
        became an exact 10**21 and rendered as "1000000000000000000000"
        instead of "1e+21"."""
        assert ev("$string($floor(1e21))") == "1e+21"
        assert ev("$string($ceil(-1e21))") == "-1e+21"

    @pytest.mark.parametrize(
        ("literal", "expected"),
        [
            ("9007199254740992", "9007199254740992"),      # 2**53 exactly
            ("9007199254740993", "9007199254740992"),      # not representable
            ("87134039170349312", "87134039170349310"),
            ("1234567890123456768", "1234567890123456800"),
            ("167054994622145265664", "167054994622145270000"),
        ],
    )
    def test_integers_beyond_2_53_use_shortest_round_trip(self, literal: str, expected: str) -> None:
        """number_to_string's integral fast path is bounded at 2**53, not
        at the 1e21 notation boundary: above 2**53 a double's EXACT
        integer value carries more digits than its shortest
        round-tripping form, and the spec renders the latter. Widening
        that bound is a tempting optimization that silently breaks these.
        """
        assert ev(f"$string({literal})") == expected


class TestRoundHighPrecision:
    @pytest.mark.parametrize(
        "expr",
        [
            "$round(1e14, 15)",
            "$round(1e15, 15)",
            "$round(1e13, 15)",
            "$round(-21816000155470.67, 15)",
            "$round(123456789.5, 15)",
        ],
    )
    def test_no_raw_decimal_exception(self, expr: str) -> None:
        """The exact-decimal fallback ran in decimal's default 28-digit
        context, so quantizing a 15-digit magnitude to 15 decimal places
        needed 30 and raised InvalidOperation, which escaped $round as a
        raw decimal exception rather than a value or a JSONata error
        code."""
        result = ev(expr)
        assert isinstance(result, (int, float))


class TestNumberParsing:
    """$number accepts a narrower grammar than Python's float()/int().
    The two regexes are ported verbatim from the reference, quirks
    included: its radix alternation is loosely bound (`^(0x..)|(0o..)|
    (0b..)$`), so a trailing space survives after a hex or octal literal
    but not after a binary one. Verified against the reference rather
    than reasoned about."""

    @pytest.mark.parametrize(
        "bad", ['" 1 "', '"+1"', '".5"', '"5."', '"1_000"', '" 0x1A"', '"-0x1A"', '"1 "', '"0b101 "']
    )
    def test_rejected_forms(self, bad: str) -> None:
        assert code(f"$number({bad})") == "D3030"

    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            ('$number("1")', 1),
            ('$number("-1.5")', -1.5),
            ('$number("1e2")', 100),
            ('$number("0x1A")', 26),
            ('$number("0o17")', 15),
            ('$number("0b101")', 5),
            # The loose-alternation quirk, reproduced deliberately.
            ('$number("0x1A ")', 26),
            ('$number("0o17 ")', 15),
        ],
    )
    def test_accepted_forms(self, expr: str, expected: object) -> None:
        assert ev(expr) == expected

    @pytest.mark.parametrize("garbage", ['"x0o17y"', '"zz0b101"'])
    def test_embedded_radix_literal_raises(self, garbage: str) -> None:
        """Deliberate divergence: the same loose alternation lets the
        reference match a radix literal in the MIDDLE of a string and
        then return NaN (its `isNaN`/`isFinite` guard covers only the
        decimal branch). Leaking NaN out of $number is a reference bug,
        so D3030 is raised instead."""
        assert code(f"$number({garbage})") == "D3030"


class TestDateTimePreEpoch:
    """datetime.fromtimestamp() / .timestamp() go through the platform C
    library, which rejects negative timestamps on Windows -- so every
    $fromMillis of a pre-1970 instant raised, for every picture string."""

    def test_pre_epoch_iso(self) -> None:
        assert ev("$fromMillis(-2208988800000)") == "1900-01-01T00:00:00.000Z"

    def test_pre_epoch_picture(self) -> None:
        assert ev('$fromMillis(-2208988800000, "[D]/[M]/[Y]")') == "1/1/1900"

    def test_negative_millis_fraction(self) -> None:
        """Python's % is already non-negative for a positive modulus, so
        the Java-style correction turned -1 ms into 1999 and printed
        ".1999"."""
        assert ev("$fromMillis(-1)") == "1969-12-31T23:59:59.999Z"

    def test_round_trip_pre_epoch(self) -> None:
        assert ev("$toMillis($fromMillis(-2208988800000))") == -2208988800000


class TestDateTimePictureComponents:
    """XPath F&O routes every integer-valued component through the same
    integer formatter $formatInteger uses; the per-component hand-written
    modifier sets silently fell back to decimal or raised."""

    @pytest.mark.parametrize(
        ("picture", "expected"),
        [
            ("[Ya]", "byv"),
            ("[YA]", "BYV"),
            ("[YI]", "MMXXIV"),
            ("[Yi]", "mmxxiv"),
            ("[Dw]", "one"),
            ("[DW]", "ONE"),
            ("[DWw]", "One"),
            ("[Dwo]", "first"),
            ("[D1o]", "1st"),
            ("[Mw]", "one"),
            ("[MI]", "I"),
        ],
    )
    def test_component_modifiers(self, picture: str, expected: str) -> None:
        assert ev(f'$fromMillis(1704067200000, "{picture}")') == expected

    def test_ordinal_year_rejected(self) -> None:
        assert code('$fromMillis(0, "[Yo]")') == "D3130"

    @pytest.mark.parametrize(
        ("millis", "picture", "expected"),
        [
            (1, "[f]", "1"), (1, "[f001]", "001"), (1, "[f0001]", "0001"),
            (12, "[f1]", "12"), (123, "[f1,1-1]", "123"), (1, "[f1,3-3]", "001"),
        ],
    )
    def test_fractional_seconds_are_an_integer(self, millis: int, picture: str, expected: str) -> None:
        """`[f]` formats the raw millisecond value, not a scaled decimal
        fraction: `[f0001]` on 1 ms is "0001", not "0010"."""
        assert ev(f'$fromMillis({millis}, "{picture}")') == expected

    @pytest.mark.parametrize(
        ("date", "expected"),
        [("1970-01-01", "1"), ("2024-02-01", "1"), ("2025-01-01", "1"), ("2024-01-08", "2")],
    )
    def test_week_in_month_first_thursday_rule(self, date: str, expected: str) -> None:
        """A month's first week is the Monday-based week containing its
        first Thursday, so when the 1st is Fri/Sat/Sun week 1 starts on
        the FOLLOWING Monday."""
        assert ev(f'$fromMillis($toMillis("{date}"), "[w]")') == expected

    @pytest.mark.parametrize(
        ("tz", "picture", "expected"),
        [
            ("+0000", "[z]", "GMT+00:00"),
            ("+0530", "[z]", "GMT+05:30"),
            ("+0530", "[z0]", "GMT+5:30"),
            ("-0800", "[z0]", "GMT-8"),
            ("+0000", "[z0]", "GMT+0"),
        ],
    )
    def test_gmt_offset(self, tz: str, picture: str, expected: str) -> None:
        assert ev(f'$fromMillis(1704067200000, "{picture}", "{tz}")') == expected

    @pytest.mark.parametrize("picture", ["[ZN]", "[zN]"])
    def test_names_timezone_rejected(self, picture: str) -> None:
        assert code(f'$fromMillis(0, "{picture}")') == "D3134"


class TestToMillisRanges:
    def test_day_rolls_over(self) -> None:
        """The reference does not validate day-of-month against the
        month's length; it constructs and lets the surplus roll over."""
        assert ev('$toMillis("2023-02-29")') == ev('$toMillis("2023-03-01")')
        assert ev('$toMillis("2023-04-31")') == ev('$toMillis("2023-05-01")')

    @pytest.mark.parametrize("bad", ["2023-13-01", "2023-00-01", "2023-01-00", "2023-01-32"])
    def test_out_of_range_rejected(self, bad: str) -> None:
        """Deliberate divergence: the reference leaks a NaN/invalid-Date
        artifact here ($type reports "object"), which is not a value worth
        reproducing."""
        assert code(f'$toMillis("{bad}")') == "D3110"

    def test_iso_week_dates_rejected(self) -> None:
        """date.fromisoformat accepts these on 3.11+; the reference's
        picture-less parser does not."""
        assert code('$toMillis("2024-W01-1")') == "D3110"


class TestSequenceAndObject:
    def test_reverse_wraps_a_scalar(self) -> None:
        """$reverse's signature is <a:a>, so a non-array argument is
        array-wrapped rather than passed through."""
        assert ev('$reverse("ab")') == ["ab"]
        assert ev("$reverse(5)") == [5]

    def test_keys_recurses_into_nested_arrays(self) -> None:
        assert ev('$keys([{"a":1},[{"b":2}]])') == ["a", "b"]

    def test_lookup_recurses_into_nested_arrays(self) -> None:
        assert ev('$lookup([{"a":1},[{"a":2}]], "a")') == [1, 2]

    def test_reduce_of_missing_is_missing(self) -> None:
        """An absent sequence is absent regardless of the initial value."""
        assert ev("$reduce(nothing, function($a,$b){$a+$b}, 5)") is jsonata.MISSING

    def test_clone(self) -> None:
        """$clone was not registered at all -- it raised T1006."""
        src = {"a": [1, {"b": 2}]}
        out = ev("$clone($$)", src)
        assert out == src
        assert out is not src
        assert out["a"][1] is not src["a"][1]

    @pytest.mark.parametrize("bad", ['"x"', "5", "true", "function($x){$x}"])
    def test_clone_rejects_non_composites(self, bad: str) -> None:
        assert code(f"$clone({bad})") == "T0410"

    def test_clone_of_missing(self) -> None:
        assert ev("$clone(nothing)") is jsonata.MISSING


class TestFunctionValueInAPathStep:
    """`a.g(...)` invokes the FIELD `g` of the step context.

    The parser produces the same `FunctionCall` node for `$o.g()` and
    `$o.$g()` once the `$` is stripped, and the translator resolved both as
    a variable/built-in -- so calling a function held in an object property
    raised T1006. `FunctionCall.is_variable` now carries the distinction.
    Every expectation below is the reference interpreter's output.
    """

    OBJ = '{"g": function($x){$x}, "h": function(){9}, "n": "ab", "z": 5, "sub": {"g": function(){7}}}'

    def test_calls_the_field(self) -> None:
        assert ev(f"($o := {self.OBJ}; $o.h())") == 9

    def test_passes_arguments(self) -> None:
        assert ev(f"($o := {self.OBJ}; $o.g(21))") == 21

    def test_argument_sees_the_step_context(self) -> None:
        assert ev(f"($o := {self.OBJ}; $o.g(n))") == "ab"

    def test_nested_step(self) -> None:
        assert ev(f"($o := {self.OBJ}; $o.sub.g())") == 7

    def test_object_literal_receiver(self) -> None:
        assert ev('{"g": function(){9}}.g()') == 9

    def test_maps_over_a_sequence(self) -> None:
        assert ev('($o := [{"g":function(){1}},{"g":function(){2}}]; $o.g())') == [1, 2]

    def test_dollar_prefix_still_means_the_variable(self) -> None:
        """`$o.$g()` calls the VARIABLE $g even when the context has its own
        `g` field -- the case that makes the distinction load-bearing."""
        assert ev('($o := {"g":function(){1}}; $g := function(){2}; $o.$g())') == 2

    def test_builtin_still_reachable_with_a_dollar(self) -> None:
        assert ev('($o := {"name":"ab"}; $o.name.$uppercase())') == "AB"

    def test_bare_builtin_name_is_a_field_not_the_builtin(self) -> None:
        """`$o.count()` is $o's own `count` field, never `$count`."""
        assert ev('($o := {"count": function(){99}}; $o.count())') == 99

    @pytest.mark.parametrize(
        ("expr", "expected"),
        [
            ('($o := {"x":1}; $o.count())', "T1005"),   # absent + built-in name -> "did you mean $count?"
            ('($o := {"x":1}; $o.g())', "T1006"),       # absent, ordinary name
            ('($o := {"g": 5}; $o.g())', "T1006"),      # present but not callable
            ('($o := 5; $o.g())', "T1006"),             # scalar context
            ('($o := null; $o.g())', "T1006"),          # JSON null context still resolves
            ("$$.g()", "T1006"),
            ('($o := [{"g":function(){1}},{"x":2}]; $o.g())', "T1006"),  # one element cannot resolve
        ],
    )
    def test_unresolvable_callee(self, expr: str, expected: str) -> None:
        assert code(expr) == expected

    @pytest.mark.parametrize("expr", ['($o := nothing; $o.g())', '($o := []; $o.g())'])
    def test_absent_context_yields_absent(self, expr: str) -> None:
        """Only a MISSING context and an empty sequence skip the call; a null
        or scalar element does not (see above)."""
        assert ev(expr) is jsonata.MISSING


class TestBuiltinContextSubstitution:
    """A $builtin() call used as a path step (or supplied with fewer args than
    its parameter count) receives the evaluation context via signature-directed
    substitution -- the reference's `parseSignature`/`validate` logic.

    These cases were previously wrong: either an IndexError crash, a T0410
    instead of T0411, or a null instead of the correct value.
    """

    def _ev(self, expr: str, data: object = None) -> object:
        return jsonata.JsonataExpressionFactory().compile(expr).evaluate(data)

    def _err(self, expr: str, data: object = None) -> str:
        try:
            jsonata.JsonataExpressionFactory().compile(expr).evaluate(data)
        except Exception as e:
            return getattr(e, "error_code", None) or type(e).__name__
        return ""

    # Focus params filled by context in a path step
    def test_uppercase_step(self) -> None:
        assert self._ev("v.$uppercase()", {"v": "ab"}) == "AB"

    def test_keys_step(self) -> None:
        assert self._ev("v.$keys()", {"v": {"a": 1}}) == "a"

    def test_substring_step(self) -> None:
        assert self._ev("v.$substring(1, 2)", {"v": "abcdef"}) == "bc"

    def test_round_step_arg_fills_value_param(self) -> None:
        # arg `1` is a number → it fills the value (first) param; no
        # context substitution. round(1, MISSING) = round-to-integer = 1.
        assert self._ev("v.$round(1)", {"v": 1.55}) == 1

    def test_round_step_zero_args_context_sub(self) -> None:
        # zero args → context (1.55) is substituted as the value param.
        assert self._ev("v.$round()", {"v": 1.55}) == 2

    def test_power_step(self) -> None:
        # both params required; 1 arg → context sub for first param.
        assert self._ev("v.$power(3)", {"v": 2}) == 8

    def test_contains_step(self) -> None:
        assert self._ev('v.$contains("b")', {"v": "abc"}) is True

    def test_replace_step(self) -> None:
        assert self._ev('v.$replace("b", "X")', {"v": "abc"}) == "aXc"

    def test_from_millis_step(self) -> None:
        assert self._ev('v.$fromMillis("[Y]")', {"v": 0}) == "1970"

    def test_to_millis_step(self) -> None:
        assert self._ev("v.$toMillis()", {"v": "2024-01-01"}) == 1704067200000

    # Type mismatch raises T0411, not T0410
    def test_uppercase_type_mismatch(self) -> None:
        assert self._err("v.$uppercase()", {"v": 5}) == "T0411"

    def test_floor_type_mismatch(self) -> None:
        assert self._err("v.$floor()", {"v": "s"}) == "T0411"

    # Direct (fully-saturated) calls are unaffected
    def test_substring_direct(self) -> None:
        assert self._ev('$substring("abcdef", 1, 2)') == "bc"

    def test_string_direct(self) -> None:
        assert self._ev("$string(true)") == "true"

    def test_round_direct(self) -> None:
        assert self._ev("$round(1.55, 1)") == 1.6


class TestParenthesizedPathHead:
    """A parenthesized expression heading a path is a VALUE; a bare quoted
    string is a FIELD NAME. The reference treats them differently and so
    do we.
    """

    def _ev(self, expr: str, data: object = None) -> object:
        return jsonata.JsonataExpressionFactory().compile(expr).evaluate(data)

    def test_paren_string_is_not_a_field(self) -> None:
        # `("ab").foo` is NOT `ab.foo`; the string has no field `foo`.
        assert self._ev('("ab").foo', {"ab": {"foo": 1}}) is jsonata.MISSING

    def test_paren_string_upper(self) -> None:
        assert self._ev('("ab").$uppercase()') == "AB"

    def test_bare_quoted_string_is_a_field(self) -> None:
        # A bare quoted string heads a path as a field name.
        assert self._ev('"ab".foo', {"ab": {"foo": 1}}) == 1

    def test_paren_concat_upper(self) -> None:
        assert self._ev('("a" & "b").$uppercase()') == "AB"

    def test_paren_subscript_upper(self) -> None:
        # Subscript sits between the head and the dot without changing the value.
        assert self._ev('("ab")[0].$uppercase()') == "AB"


class TestNestedPathWildcard:
    """A parenthesised sub-path `(*.y)` is evaluated with the reference's
    re-split semantics: the current sequence element is itself iterated before
    the wildcard step runs, not treated as a whole node.

    Previously the optimizer folded `x.(*.y)` into `x.*.y` (identical code),
    losing that per-element evaluation boundary.
    """

    def _ev(self, expr: str, data: object = None) -> object:
        return jsonata.JsonataExpressionFactory().compile(expr).evaluate(data)

    # Folded vs unfolded must give different answers for nested arrays
    def test_dot_wildcard_field_nested(self) -> None:
        # x.*.y applies * to [[{y:1}]], gets [{y:1}], field y=1
        # x.(*.y) applies *.y to each element [{y:1}] via re-split: * of {y:1}=1, y of 1=undefined
        assert self._ev("x.*.y", {"x": [[{"y": 1}]]}) == 1
        assert self._ev("x.(*.y)", {"x": [[{"y": 1}]]}) is jsonata.MISSING

    def test_dot_dollar_wildcard_nested(self) -> None:
        # x.* on [[1,2]] = [1,2];  x.($.*) re-splits [1,2] to 1,2 then wildcard(scalar)=undefined
        assert self._ev("x.*", {"x": [[1, 2]]}) == [1, 2]
        assert self._ev("x.($.*)", {"x": [[1, 2]]}) is jsonata.MISSING

    def test_non_nested_wildcard_unchanged(self) -> None:
        # Flat dict elements: x.(*.y) and x.*.y agree
        assert self._ev("x.(*.y)", {"x": [{"y": 1}, {"y": 2}]}) is jsonata.MISSING
        assert self._ev("x.*.y", {"x": [{"y": 1}, {"y": 2}]}) is jsonata.MISSING

    def test_dollar_y_still_correct(self) -> None:
        # x.($.y) with [[{y:1}]] - accidental correctness preserved
        assert self._ev("x.($.y)", {"x": [[{"y": 1}]]}) == 1


class TestSpread:
    """$spread: object input → singleton-collapses a single-key result (matching
    the reference's non-cons sequence); array input → preserves the array.
    Previously the two behaviours were swapped.
    """

    def _ev(self, expr: str, data: object = None) -> object:
        return jsonata.JsonataExpressionFactory().compile(expr).evaluate(data)

    def test_spread_single_key_object(self) -> None:
        assert self._ev('$spread({"a":1})') == {"a": 1}

    def test_spread_multi_key_object(self) -> None:
        assert self._ev('$spread({"a":1,"b":2})') == [{"a": 1}, {"b": 2}]

    def test_spread_array_single(self) -> None:
        assert self._ev('$spread([{"a":1}])') == [{"a": 1}]

    def test_spread_array_multi(self) -> None:
        assert self._ev('$spread([{"a":1},{"b":2}])') == [{"a": 1}, {"b": 2}]

    def test_spread_via_context(self) -> None:
        # $spread() with context substitution
        assert self._ev("v.$spread()", {"v": {"a": 1}}) == {"a": 1}

