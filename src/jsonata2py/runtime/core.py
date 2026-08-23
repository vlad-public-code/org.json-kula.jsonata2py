"""Core runtime support for generated JSONata expression modules.

Ported from org.json_kula.jsonata_jvm.runtime.JsonataRuntime.

Generated modules do `from jsonata2py.runtime import *` so the
call sites here read as plain function calls (see runtime/__init__.py).

Sequence semantics: JSONata treats the document as a stream of values.
Many operations automatically map over arrays ("sequence mapping"). field,
wildcard, and descendant implement this by recursively visiting every
element of a list input and collecting results.

Undefined/missing values: JSONata's "undefined" is represented by the
MISSING sentinel (values.py). Operations on missing values propagate the
missing value rather than raising (arithmetic excepted -- see individual
functions). None is JSON null, a real value, and is never treated as
missing here (D1).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from decimal import ROUND_HALF_UP, Context, Decimal
from types import ModuleType
from typing import Any, cast

from ..errors import _RuntimeEvaluationError as RuntimeEvaluationError
from . import context as _ctx
from .values import MISSING, JLambda, JRegex, Preserved, is_function, is_number, is_regex

__all__ = [
    # value model re-exports
    "MISSING",
    "NULL",
    "JLambda",
    "JRegex",
    "Preserved",
    "RangeHolder",
    # arithmetic
    "add",
    "and_",
    "apply_step",
    "array",
    "array_of",
    "begin_evaluation",
    "call_bound_function",
    "clamp_index",
    "coalesce",
    "collect_pos_tuples",
    # string concat / comparisons / boolean logic
    "concat",
    "deadline_guard",
    "deep_equals",
    "descendant",
    # internal helpers referenced by generated code
    "discard",
    "div_d",
    "divide",
    "dynamic_filter",
    "each_indexed",
    "element_callback",
    "elvis",
    "end_evaluation",
    "eq",
    # factory / path navigation
    "field",
    "filter_",
    "fn_abs",
    "fn_append",
    "fn_apply",
    "fn_apply_tco",
    "fn_arg_count_error",
    "fn_arity_error",
    "fn_assert",
    "fn_average",
    "fn_average_field",
    "fn_base64decode",
    "fn_base64encode",
    "fn_boolean",
    "fn_ceil",
    "fn_collect_pairs",
    "fn_collect_triples",
    "fn_contains",
    # array / sequence builtins
    "fn_count",
    "fn_count_field",
    "fn_count_field_eq",
    "fn_count_filter",
    "fn_decodeUrl",
    "fn_decodeUrlComponent",
    "fn_distinct",
    "fn_each",
    "fn_encodeUrl",
    "fn_encodeUrlComponent",
    "fn_error",
    "fn_eval",
    "fn_exists",
    "fn_filter",
    "fn_filter_indexed",
    "fn_flatten",
    "fn_floor",
    "fn_formatBase",
    "fn_formatInteger",
    "fn_formatNumber",
    "fn_fromMillis",
    "fn_join",
    # object builtins
    "fn_keys",
    "fn_length",
    "fn_length_ctx",
    "fn_lookup",
    "fn_lowercase",
    "fn_map",
    "fn_map_indexed",
    "fn_match",
    "fn_max",
    "fn_max_field",
    "fn_merge",
    "fn_millis",
    "fn_min",
    "fn_min_field",
    "fn_not",
    # date/time
    "fn_now",
    "fn_number",
    "fn_pad",
    "fn_parseInteger",
    # chain / lambda / regex
    "fn_pipe",
    "fn_power",
    "fn_random",
    "fn_reduce",
    "fn_replace",
    "fn_reverse",
    "fn_round",
    "fn_shuffle",
    "fn_sift",
    "fn_single",
    "fn_single_indexed",
    "fn_sort",
    "fn_sort_by_ordering_key",
    "fn_sort_comparator",
    "fn_split",
    "fn_spread",
    "fn_sqrt",
    # type coercion / scalar builtins
    "fn_string",
    "fn_substring",
    "fn_substringAfter",
    "fn_substringAfter_ctx",
    "fn_substringBefore",
    "fn_substringBefore_ctx",
    "fn_sum",
    "fn_sum_field",
    "fn_throw",
    "fn_toMillis",
    "fn_transform",
    "fn_trim",
    "fn_type",
    # string builtins
    "fn_uppercase",
    "fn_values",
    "fn_zip",
    "force_array",
    "ge",
    "gt",
    "in_",
    "is_evaluation_active",
    "is_function",
    "is_lambda_token",
    "is_missing",
    "is_number",
    "is_regex",
    "is_regex_token",
    "is_truthy",
    "json_encode_compact",
    "json_encode_pretty",
    "lambda_arity",
    "lambda_value",
    "le",
    "lt",
    "map_constructor_step",
    "map_constructor_step_flat",
    "map_step",
    "merge_group_by_objects",
    "missing",
    "mod_d",
    "modulo",
    "mul_d",
    "multiply",
    "ne",
    "neg_dn",
    "negate",
    "next_counter",
    "num_node",
    "num_val_l",
    "num_val_r",
    "num_wrap",
    "number_to_string",
    "object_",
    "object_of",
    "or_",
    # constructors
    "pack_args",
    "preserve_array",
    "range_",
    "range_flatten",
    "range_subscript",
    "regex_value",
    # bindings support (thin delegations to context.py)
    "resolve_binding",
    "sanitize_for_string",
    "subscript",
    "subtract",
    "to_number",
    "to_text",
    "tuple2",
    "tuple_callback",
    "unwrap",
    "wildcard",
]

# JSONata "null" literal is Python None; MISSING is the "undefined" sentinel.
NULL = None


# =============================================================================
# Factory / kind helpers
# =============================================================================


def missing(n: Any) -> bool:
    """Reference identity check against the MISSING singleton -- called on
    every operand of every operation, so this stays a pointer compare."""
    return n is MISSING


def is_missing(n: Any) -> bool:
    return n is MISSING


_KINDS = {
    type(None): "null",
    bool: "boolean",
    int: "number",
    float: "number",
    str: "string",
    list: "array",
    dict: "object",
}


def _kind(v: Any) -> str:
    k = _KINDS.get(type(v))
    if k is not None:
        return k
    return _kind_slow(v)


def _kind_slow(v: Any) -> str:
    if is_number(v):
        return "number"
    if is_function(v) or is_regex(v):
        return "function"
    return "other"


# =============================================================================
# Path navigation
# =============================================================================


def field(node: Any, name: str) -> Any:
    """Navigates to name on node, automatically mapping over arrays
    (JSONata sequence semantics).

    The #1 hot spot of the whole library: a path step is a field() call,
    and a path step over a sequence is one more per element. Both shapes
    get an exact-type fast path here, with the general algorithm kept in
    _field_over_sequence for everything they do not cover.
    """
    if node.__class__ is dict:
        return node.get(name, MISSING)
    if node is None or node is MISSING:
        return MISSING
    if node.__class__ is list:
        # Flat list of plain dicts -- what essentially all real JSON input
        # looks like. Inlining the per-element navigate/flatten/collect
        # avoids a recursive field() call and an _append_to_sequence()
        # call per element. Anything else (nested lists, MISSING members,
        # dict/list subclasses such as Preserved) hands off unchanged.
        out: list[Any] = []
        append = out.append
        for elem in node:
            if elem.__class__ is not dict:
                return _field_over_sequence(node, name)
            val = elem.get(name, MISSING)
            if val is MISSING:
                continue
            vt = val.__class__
            if vt is str or vt is int or vt is float:
                append(val)
            elif vt is list or isinstance(val, list):
                # One level of sequence flattening, as _append_to_sequence
                # does -- isinstance, so Preserved and other list
                # subclasses still flatten exactly as they did before.
                out.extend(val)
            else:
                append(val)
        n = len(out)
        if n == 0:
            return MISSING
        return out[0] if n == 1 else out
    if isinstance(node, (dict, list)):
        # dict/list subclasses: correctness over speed.
        return node.get(name, MISSING) if isinstance(node, dict) else _field_over_sequence(node, name)
    return MISSING


def _field_over_sequence(node: Any, name: str) -> Any:
    """field()'s general sequence algorithm, for lists the fast path in
    field() declines to handle."""
    single: Any = None
    result: list[Any] | None = None
    have_single = False
    for elem in node:
        val = field(elem, name)
        if val is MISSING:
            continue
        if result is not None:
            _append_to_sequence(result, val)
        elif not have_single and not isinstance(val, list):
            single = val
            have_single = True
        else:
            result = []
            if have_single:
                result.append(single)
            have_single = False
            _append_to_sequence(result, val)
    if result is not None:
        return _unwrap_list(result)
    return single if have_single else MISSING


def wildcard(node: Any) -> Any:
    """Returns all field values of an object, or maps over an array."""
    if node is None or node is MISSING:
        return MISSING
    if isinstance(node, list):
        result: list[Any] = []
        for elem in node:
            if isinstance(elem, dict):
                _append_to_sequence(result, wildcard(elem))
            elif elem is not MISSING:
                result.append(elem)
        return _unwrap_list(result)
    if isinstance(node, dict):
        result = []
        for v in node.values():
            _append_to_sequence(result, v)
        return _unwrap_list(result)
    return MISSING


def descendant(node: Any) -> Any:
    """Recursively collects all descendant values (depth-first)."""
    if node is None or node is MISSING:
        return MISSING
    result: list[Any] = []
    _collect_descendants(node, result)
    return _unwrap_list(result)


def _collect_descendants(node: Any, acc: list[Any]) -> None:
    if isinstance(node, list):
        for elem in node:
            _collect_descendants(elem, acc)
    elif isinstance(node, dict) and node:
        acc.append(node)
        for v in node.values():
            _collect_descendants(v, acc)


def force_array(node: Any) -> Any:
    """Forces node to be a list, wrapping it in a single-element list if
    not already one. Implements the expr[] operator."""
    if node is None or node is MISSING:
        return MISSING
    if isinstance(node, list):
        return node
    return [node]


def filter_(seq: Any, predicate: Callable[[Any], Any]) -> Any:
    """Filters seq by predicate, preserving elements for which the
    predicate returns a truthy value."""
    predicate = deadline_guard(predicate)
    if seq is None or seq is MISSING:
        return MISSING
    if not isinstance(seq, list):
        return seq if is_truthy(predicate(seq)) else MISSING
    result: list[Any] | None = None
    single: Any = None
    have_single = False
    for elem in seq:
        if not is_truthy(predicate(elem)):
            continue
        if result is not None:
            result.append(elem)
        elif not have_single:
            single = elem
            have_single = True
        else:
            result = [single, elem]
            have_single = False
    if result is not None:
        return result
    return single if have_single else MISSING


def dynamic_filter(seq: Any, predicate: Callable[[Any], Any]) -> Any:
    """Dynamic filter: probes the predicate with MISSING to determine
    mode. If the result is a number -> index subscript; otherwise ->
    boolean filter."""
    if seq is None or seq is MISSING:
        return MISSING
    probe = predicate(MISSING)
    if probe is not None and probe is not MISSING:
        if is_number(probe):
            return subscript(seq, probe)
        if isinstance(probe, list):
            all_ints = all(is_number(idx) for idx in probe)
            if all_ints:
                size = len(seq) if isinstance(seq, list) else 1
                indices: set[int] = set()
                for idx in probe:
                    i = int(idx)
                    actual = size + i if i < 0 else i
                    if 0 <= actual < size:
                        indices.add(actual)
                result = []
                for i in sorted(indices):
                    val = seq[i] if isinstance(seq, list) else (seq if i == 0 else MISSING)
                    if val is not MISSING:
                        result.append(val)
                return _unwrap_list(result)
    return filter_(seq, predicate)


def subscript(seq: Any, index: Any) -> Any:
    """Returns the element at index (zero-based, negatives count from
    end)."""
    if seq is None or seq is MISSING:
        return MISSING
    i = int(to_number(index))
    if not isinstance(seq, list):
        return seq if i in (0, -1) else MISSING
    size = len(seq)
    actual = size + i if i < 0 else i
    return seq[actual] if 0 <= actual < size else MISSING


def range_subscript(seq: Any, from_: Any, to: Any) -> Any:
    """Returns a sub-array containing elements at indices from through to
    (inclusive, zero-based, negatives count from end)."""
    if seq is None or seq is MISSING:
        return MISSING
    if not isinstance(seq, list):
        f = int(to_number(from_))
        t = int(to_number(to))
        norm_f = 1 + f if f < 0 else f
        norm_t = 1 + t if t < 0 else t
        return seq if (norm_f <= 0 and norm_t >= 0) else MISSING
    f = int(to_number(from_))
    t = int(to_number(to))
    size = len(seq)
    actual_f = size + f if f < 0 else f
    actual_t = size + t if t < 0 else t
    return list(seq[max(0, actual_f) : min(size - 1, actual_t) + 1])


def apply_step(node: Any, fn: Callable[[Any], Any]) -> Any:
    """Applies fn with the element as the new context."""
    if node is None or node is MISSING:
        return MISSING
    return fn(node)


def map_step(node: Any, fn: Callable[[Any], Any]) -> Any:
    """Maps fn over every element of a sequence, collecting non-missing
    results. Used for subscript steps inside path expressions."""
    fn = deadline_guard(fn)
    if node is None or node is MISSING:
        return MISSING
    if isinstance(node, list):
        result: list[Any] = []
        for elem in node:
            val = fn(elem)
            if val is not MISSING:
                _append_to_sequence(result, val)
        return _unwrap_list(result)
    return fn(node)


def map_constructor_step(node: Any, fn: Callable[[Any], Any]) -> Any:
    """Maps fn over every element of a sequence and collects results
    without flattening. Used for array/object constructor steps inside
    path expressions."""
    fn = deadline_guard(fn)
    if node is None or node is MISSING:
        return MISSING
    if isinstance(node, list):
        result: list[Any] = []
        for elem in node:
            val = fn(elem)
            if val is not MISSING:
                result.append(_unwrap_preserve(val))
        return result if result else MISSING
    val = fn(node)
    if val is MISSING:
        return MISSING
    return _unwrap_preserve(val)


def map_constructor_step_flat(node: Any, fn: Callable[[Any], Any]) -> Any:
    """Variant of map_constructor_step for the non-preserve (flatten)
    case."""
    if node is None or node is MISSING:
        return MISSING
    if isinstance(node, list):
        result: list[Any] = []
        for elem in node:
            val = fn(elem)
            if val is not MISSING:
                _append_to_sequence(result, val)
        return _unwrap_list(result)
    val = fn(node)
    return MISSING if val is MISSING else val


def _unwrap_preserve(node: Any) -> Any:
    return list(node) if isinstance(node, Preserved) else node


# =============================================================================
# Arithmetic
# =============================================================================


def _require_numeric_operands(a: Any, b: Any, operator: str) -> None:
    if a is not MISSING and not is_number(a):
        raise RuntimeEvaluationError("T2001", f"The left side of the {operator} operator must evaluate to a number")
    if b is not MISSING and not is_number(b):
        raise RuntimeEvaluationError("T2002", f"The right side of the {operator} operator must evaluate to a number")


def add(a: Any, b: Any) -> Any:
    _require_numeric_operands(a, b, "+")
    if a is MISSING or b is MISSING:
        return MISSING
    return num_node(float(a) + float(b))


def subtract(a: Any, b: Any) -> Any:
    _require_numeric_operands(a, b, "-")
    if a is MISSING or b is MISSING:
        return MISSING
    return num_node(float(a) - float(b))


def multiply(a: Any, b: Any) -> Any:
    _require_numeric_operands(a, b, "*")
    if a is MISSING or b is MISSING:
        return MISSING
    result = float(a) * float(b)
    if math.isnan(result) or math.isinf(result):
        raise RuntimeEvaluationError("D1001", "Numeric value out of range")
    return num_node(result)


def divide(a: Any, b: Any) -> Any:
    _require_numeric_operands(a, b, "/")
    if a is MISSING or b is MISSING:
        return MISSING
    numer = float(a)
    denom = float(b)
    if denom == 0:
        if math.isinf(numer):
            raise RuntimeEvaluationError("D1001", "Numeric value out of range")
        return num_node(math.inf if numer >= 0 else -math.inf)
    if math.isinf(denom):
        raise RuntimeEvaluationError("D1001", "Numeric value out of range")
    if math.isnan(denom):
        raise RuntimeEvaluationError("D1001", "Numeric value out of range")
    result = numer / denom
    if math.isnan(result):
        raise RuntimeEvaluationError("D1001", "Numeric value out of range")
    return num_node(result)


def modulo(a: Any, b: Any) -> Any:
    if a is not MISSING and not is_number(a):
        raise RuntimeEvaluationError("T2001", "The left side of the % operator must evaluate to a number")
    if b is not MISSING and not is_number(b):
        raise RuntimeEvaluationError("T2002", "The right side of the % operator must evaluate to a number")
    if a is MISSING or b is MISSING:
        return MISSING
    denom = float(b)
    if denom == 0:
        raise RuntimeEvaluationError("D1001", "Division by zero")
    return num_node(math.fmod(float(a), denom))


def negate(a: Any) -> Any:
    if a is MISSING:
        return MISSING
    if not is_number(a):
        raise RuntimeEvaluationError("D1002", "The operand of the - operator must evaluate to a number")
    return num_node(-float(a))


# =============================================================================
# Primitive arithmetic helpers (used by generated numeric-specialised code)
# =============================================================================

_NAN = math.nan


def num_val_l(n: Any, op: str) -> float:
    """Extracts a float for use in fused arithmetic chains. Returns NaN as
    a "missing" sentinel; raises T2001 if present but non-numeric.

    Exact-type first: this runs on both operands of every arithmetic
    operation, and the answer is nearly always settled by the type alone.
    bool is an int subclass but not a JSONata number, and
    True.__class__ is bool, so it correctly falls through to is_number().
    """
    t = n.__class__
    if t is float:
        return n  # type: ignore[no-any-return]
    if t is int:
        return float(n)
    if n is MISSING:
        return _NAN
    if not is_number(n):
        raise RuntimeEvaluationError("T2001", f"The left side of the {op} operator must evaluate to a number")
    return float(n)


def num_val_r(n: Any, op: str) -> float:
    """Like num_val_l but raises T2002 (right-operand error)."""
    t = n.__class__
    if t is float:
        return n  # type: ignore[no-any-return]
    if t is int:
        return float(n)
    if n is MISSING:
        return _NAN
    if not is_number(n):
        raise RuntimeEvaluationError("T2002", f"The right side of the {op} operator must evaluate to a number")
    return float(n)


def num_wrap(v: float) -> Any:
    """Wraps a primitive float result back into a value. NaN (the missing
    sentinel used inside arithmetic chains) becomes MISSING."""
    if v != v:  # NaN
        return MISSING
    return num_node(v)


def mul_d(a: float, b: float) -> float:
    if a != a or b != b:
        return _NAN
    result = a * b
    if result != result or math.isinf(result):
        raise RuntimeEvaluationError("D1001", "Numeric value out of range")
    return result


def div_d(a: float, b: float) -> float:
    if a != a or b != b:
        return _NAN
    if b == 0:
        if math.isinf(a):
            raise RuntimeEvaluationError("D1001", "Numeric value out of range")
        return math.inf if a >= 0 else -math.inf
    if math.isinf(b):
        raise RuntimeEvaluationError("D1001", "Numeric value out of range")
    result = a / b
    if result != result:
        raise RuntimeEvaluationError("D1001", "Numeric value out of range")
    return result


def mod_d(a: float, b: float) -> float:
    if a != a or b != b:
        return _NAN
    if b == 0:
        raise RuntimeEvaluationError("D1001", "Division by zero")
    return math.fmod(a, b)


def neg_dn(n: Any) -> float:
    if n is MISSING:
        return _NAN
    if not is_number(n):
        raise RuntimeEvaluationError("D1002", "The operand of the - operator must evaluate to a number")
    return -float(n)


def fn_throw(code: str, message: str) -> Any:
    raise RuntimeEvaluationError(code, message)


def fn_arity_error(name: str, expected: int, actual: int) -> Any:
    raise RuntimeEvaluationError(
        "T0410", f"Function ${name} requires {expected} argument(s) but received {actual}"
    )


def fn_arg_count_error(name: str, min_: int, actual: int) -> Any:
    raise RuntimeEvaluationError(
        "T0411", f"Function ${name} requires at least {min_} argument(s) but received {actual}"
    )


_LONG_MIN = -9223372036854775808
_LONG_MAX = 9223372036854775807


def num_node(v: float) -> Any:
    """Returns an int when v is a whole number within int64 range, else
    float.

    float.is_integer() is already False for NaN and both infinities, so the
    separate NaN/isinf guards this used to carry were redundant.

    Callers do NOT all pass a float, despite the annotation -- int is
    assignable to a `float` parameter, so the type checker cannot catch it.
    That matters because `int.is_integer()` only exists on Python 3.12+; on
    the declared 3.11 floor an int argument raises AttributeError. The int
    branch below handles that explicitly, reproducing exactly what 3.12+
    computed for the same input.
    """
    if isinstance(v, int):
        return int(v) if _LONG_MIN <= v <= _LONG_MAX else v
    if v.is_integer() and _LONG_MIN <= v <= _LONG_MAX:
        return int(v)
    return v


# =============================================================================
# String concatenation
# =============================================================================


def concat(a: Any, b: Any) -> Any:
    if a is MISSING and b is MISSING:
        return MISSING
    sa = "" if a is MISSING else to_text(a)
    sb = "" if b is MISSING else to_text(b)
    return sa + sb


# =============================================================================
# Comparisons
# =============================================================================


def eq(a: Any, b: Any) -> bool:
    if a is MISSING or b is MISSING:
        return False
    return _deep_equals(a, b)


def ne(a: Any, b: Any) -> bool:
    if a is MISSING or b is MISSING:
        return False
    return not _deep_equals(a, b)


def deep_equals(a: Any, b: Any) -> bool:
    return _deep_equals(a, b)


def _deep_equals(a: Any, b: Any) -> bool:
    """Recursive structural equality that compares by kind before value
    (D1), so True/1 and False/0 are never conflated."""
    ta = a.__class__
    if ta is b.__class__ and (ta is str or ta is bool):
        return bool(a == b)
    ka = _kind(a)
    kb = _kind(b)
    if ka == kb:
        if ka == "string":
            return bool(a == b)
        if ka == "number":
            return bool(float(a) == float(b))
        if ka == "boolean":
            return bool(a == b)
        if ka == "null":
            return True
        # array/object/function fall through to the recursive walk below
    if is_number(a) and is_number(b):
        return float(a) == float(b)
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return False
        return all(_deep_equals(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, dict) and isinstance(b, dict):
        if len(a) != len(b):
            return False
        for k, v in a.items():
            if k not in b:
                return False
            bv = b[k]
            if bv is MISSING:
                return False
            if not _deep_equals(v, bv):
                return False
        return True
    return False


def _ordering_ok(n: Any) -> bool:
    return n is MISSING or is_number(n) or isinstance(n, str)


def _ordering_error(a: Any, b: Any) -> RuntimeEvaluationError:
    if _ordering_ok(a) and _ordering_ok(b):
        return RuntimeEvaluationError("T2009", "operands of ordering operator must be of the same type")
    return RuntimeEvaluationError("T2010", "operands of ordering operator must be numeric or string values")


def lt(a: Any, b: Any) -> Any:
    ta = a.__class__
    tb = b.__class__
    if (ta is float or ta is int) and (tb is float or tb is int):
        return float(a) < float(b)
    if ta is str and tb is str:
        return a < b
    if not _ordering_ok(a) or not _ordering_ok(b):
        raise _ordering_error(a, b)
    if a is MISSING or b is MISSING:
        return MISSING
    if is_number(a) and is_number(b):
        return float(a) < float(b)
    if isinstance(a, str) and isinstance(b, str):
        return a < b
    raise _ordering_error(a, b)


def le(a: Any, b: Any) -> Any:
    ta = a.__class__
    tb = b.__class__
    if (ta is float or ta is int) and (tb is float or tb is int):
        return float(a) <= float(b)
    if ta is str and tb is str:
        return a <= b
    if not _ordering_ok(a) or not _ordering_ok(b):
        raise _ordering_error(a, b)
    if a is MISSING or b is MISSING:
        return MISSING
    if is_number(a) and is_number(b):
        return float(a) <= float(b)
    if isinstance(a, str) and isinstance(b, str):
        return a <= b
    raise _ordering_error(a, b)


def gt(a: Any, b: Any) -> Any:
    ta = a.__class__
    tb = b.__class__
    if (ta is float or ta is int) and (tb is float or tb is int):
        return float(a) > float(b)
    if ta is str and tb is str:
        return a > b
    if not _ordering_ok(a) or not _ordering_ok(b):
        raise _ordering_error(a, b)
    if a is MISSING or b is MISSING:
        return MISSING
    if is_number(a) and is_number(b):
        return float(a) > float(b)
    if isinstance(a, str) and isinstance(b, str):
        return a > b
    raise _ordering_error(a, b)


def ge(a: Any, b: Any) -> Any:
    ta = a.__class__
    tb = b.__class__
    if (ta is float or ta is int) and (tb is float or tb is int):
        return float(a) >= float(b)
    if ta is str and tb is str:
        return a >= b
    if not _ordering_ok(a) or not _ordering_ok(b):
        raise _ordering_error(a, b)
    if a is MISSING or b is MISSING:
        return MISSING
    if is_number(a) and is_number(b):
        return float(a) >= float(b)
    if isinstance(a, str) and isinstance(b, str):
        return a >= b
    raise _ordering_error(a, b)


# =============================================================================
# Boolean logic
# =============================================================================


def and_(a: Any, b: Callable[[], Any]) -> bool:
    if not is_truthy(a):
        return False
    return is_truthy(b())


def or_(a: Any, b: Callable[[], Any]) -> bool:
    if is_truthy(a):
        return True
    return is_truthy(b())


def elvis(left: Any, right: Any) -> Any:
    """Returns left if truthy, otherwise right."""
    return left if is_truthy(left) else right


def coalesce(left: Any, right: Any) -> Any:
    """Returns left if defined (not MISSING), otherwise right."""
    return right if left is MISSING else left


def in_(item: Any, seq: Any) -> bool:
    if item is MISSING or seq is MISSING:
        return False
    if isinstance(seq, list):
        return any(eq(item, elem) for elem in seq)
    return eq(item, seq)


def is_truthy(n: Any) -> bool:
    """JSONata boolean coercion rules: false/null/missing -> false; 0/""
    -> false; empty object -> false; array is truthy iff any member is
    truthy; function -> false; everything else -> true.

    The null/MISSING test stays first, ahead of the type dispatch: it is
    the single most common argument and the cheapest possible answer, and
    demoting it below a chain of type identity checks measurably slowed
    it down.
    """
    if n is None or n is MISSING:
        return False
    t = n.__class__
    if t is bool:
        return n  # type: ignore[no-any-return]
    if t is str or t is dict:
        return len(n) > 0
    if t is int or t is float:
        return n != 0  # type: ignore[no-any-return]
    if t is list:
        return any(is_truthy(x) for x in n)
    return _is_truthy_uncommon(n)


def _is_truthy_uncommon(n: Any) -> bool:
    """Subclasses of the built-in types, plus function/regex values."""
    if isinstance(n, bool):
        return n
    if is_number(n):
        return bool(n != 0)
    if isinstance(n, str):
        return len(n) > 0
    if isinstance(n, dict):
        return len(n) > 0
    if isinstance(n, list):
        return any(is_truthy(x) for x in n)
    # Function/regex values are falsy per the JSONata spec
    return not (is_function(n) or is_regex(n))


# =============================================================================
# Constructors
# =============================================================================


def pack_args(*elements: Any) -> list[Any]:
    """Packs function arguments into a list WITHOUT flattening -- unlike
    array_of, which flattens list values. None (JSON null, D1) is a real
    argument value and is passed through as-is; only an actually-absent
    slot should ever be MISSING, which callers pass explicitly."""
    return list(elements)


def array(*elements: Any) -> list[Any]:
    """Creates a list from the given elements, skipping missing values,
    flattening one level of list-valued elements."""
    result: list[Any] = []
    for e in elements:
        if e is MISSING:
            continue
        if isinstance(e, list):
            result.extend(e)
        else:
            result.append(e)
    return result


def tuple2(a: Any, b: Any) -> list[Any]:
    """Guaranteed 2-element [a, b] list without flattening inner lists."""
    return [a, b]


def collect_pos_tuples(seq: Any, elems_fn: Callable[[Any], Any]) -> Any:
    """Collects [sortItem, posIndex] tuples for a position-aware global
    sort."""
    if seq is None or seq is MISSING:
        return MISSING
    size = len(seq) if isinstance(seq, list) else 1
    result: list[Any] = []
    for i in range(size):
        item = seq[i] if isinstance(seq, list) else seq
        elems = elems_fn([item, i])
        if elems is None or elems is MISSING:
            continue
        if isinstance(elems, list):
            for e in elems:
                if e is not MISSING:
                    result.append([e, i])
        else:
            result.append([elems, i])
    return result if result else MISSING


class RangeHolder:
    """Marker to indicate an array element should be flattened (from a
    range expression)."""

    __slots__ = ("from_", "to")

    def __init__(self, from_: int, to: int) -> None:
        self.from_ = from_
        self.to = to


def preserve_array(arr: Any) -> Any:
    """Marks arr as a single sequence element, so that array_of adds it
    whole instead of merging its contents."""
    return Preserved(arr) if isinstance(arr, list) else arr


def range_flatten(from_: int, to: int) -> RangeHolder:
    return RangeHolder(from_, to)


def array_of(*elements: Any) -> list[Any]:
    """Creates a list from the given elements, handling RangeHolder
    markers to flatten range expressions and Preserved wrappers to
    preserve nested array constructors. List values are flattened into
    the result (JSONata sequence rule), unless wrapped with
    preserve_array."""
    result: list[Any] = []
    for e in elements:
        # Note: None here means JSON null (D1), a real element -- only
        # MISSING is skipped. (Java's `if (e == null) continue` is a
        # defensive check against a Java-level null slot, which has no
        # equivalent here since JSON null is represented directly as None.)
        if isinstance(e, RangeHolder):
            result.extend(range(e.from_, e.to + 1))
        elif isinstance(e, Preserved):
            result.append(list(e))
        elif isinstance(e, list):
            result.extend(e)
        elif e is MISSING:
            continue
        else:
            result.append(e)
    return result


def object_of(keys: list[str], values: list[Any]) -> dict[str, Any]:
    """Builds an object whose keys are all known at compile time."""
    result: dict[str, Any] = {}
    for k, value in zip(keys, values, strict=True):
        # None here means JSON null (D1), a real value -- only MISSING
        # (an unpopulated slot) is skipped.
        if value is MISSING:
            continue
        if k in result:
            raise RuntimeEvaluationError("D1009", f'Multiple key definitions evaluate to the same key: "{k}"')
        result[k] = value
    return result


def object_(*key_value_pairs: Any) -> dict[str, Any]:
    if len(key_value_pairs) % 2 != 0:
        raise RuntimeEvaluationError("T1001", "object() requires an even number of arguments")
    result: dict[str, Any] = {}
    for i in range(0, len(key_value_pairs), 2):
        key = key_value_pairs[i]
        val = key_value_pairs[i + 1]
        if key is not MISSING and val is not MISSING:
            if not isinstance(key, str):
                raise RuntimeEvaluationError(
                    "T1003", "The key expression of an object component must evaluate to a string"
                )
            if key in result:
                raise RuntimeEvaluationError(
                    "D1009", f'Multiple key definitions evaluate to the same key: "{key}"'
                )
            result[key] = val
    return result


def range_(from_: Any, to: Any) -> list[Any]:
    """Creates an integer range list [from, from+1, ..., to]."""
    if from_ is MISSING:
        if to is not MISSING and not is_number(to):
            raise RuntimeEvaluationError(
                "T2004", "The right side of the range operator (..) must evaluate to an integer"
            )
        return []
    if to is MISSING:
        if not is_number(from_):
            raise RuntimeEvaluationError(
                "T2003", "The left side of the range operator (..) must evaluate to an integer"
            )
        return []
    if not is_number(from_):
        raise RuntimeEvaluationError("T2003", "The left side of the range operator (..) must evaluate to an integer")
    if not is_number(to):
        raise RuntimeEvaluationError("T2004", "The right side of the range operator (..) must evaluate to an integer")
    fd = float(from_)
    td = float(to)
    if fd != math.floor(fd):
        raise RuntimeEvaluationError("T2003", "The left side of the range operator (..) must be an integer")
    if td != math.floor(td):
        raise RuntimeEvaluationError("T2004", "The right side of the range operator (..) must be an integer")
    f = int(fd)
    t = int(td)
    if t - f >= 10_000_000:
        raise RuntimeEvaluationError("D2014", "The range expression generates too many values")
    result: list[Any] = []
    i = f
    while i <= t:
        if (i & 0xFFF) == 0:
            _ctx.check_timeout()
        result.append(i)
        i += 1
    return result


# =============================================================================
# Built-in functions -- type coercion
# =============================================================================


def fn_string(arg: Any, prettify: Any = MISSING) -> Any:
    if arg is MISSING:
        return MISSING
    if prettify is not MISSING and not isinstance(prettify, bool):
        raise RuntimeEvaluationError("T0410", "Argument 2 of function $string must be a boolean")
    if prettify is MISSING or not is_truthy(prettify):
        return _fn_string_plain(arg)
    from .strings import builtins as _string_builtins

    return _string_builtins.fn_string_prettify(arg)


def _fn_string_plain(arg: Any) -> Any:
    if is_number(arg) and (math.isinf(arg) or math.isnan(arg)):
        raise RuntimeEvaluationError("D3001", "Attempting to invoke a non-numeric value as a numeric function")
    if isinstance(arg, (dict, list)):
        _check_no_infinity(arg)
    return to_text(arg)


def _check_no_infinity(node: Any) -> None:
    if is_number(node) and (math.isinf(node) or math.isnan(node)):
        raise RuntimeEvaluationError("D1001", "Numeric value out of range")
    if isinstance(node, list):
        for e in node:
            _check_no_infinity(e)
    if isinstance(node, dict):
        for v in node.values():
            _check_no_infinity(v)


def fn_number(arg: Any) -> Any:
    from .numeric import builtins as _numeric_builtins

    return _numeric_builtins.fn_number(arg)


def fn_boolean(arg: Any) -> Any:
    if arg is MISSING:
        return MISSING
    return is_truthy(arg)


def fn_not(arg: Any) -> Any:
    if arg is MISSING:
        return MISSING
    return not is_truthy(arg)


def fn_type(arg: Any) -> Any:
    if arg is MISSING:
        return MISSING
    if is_function(arg) or is_regex(arg):
        return "function"
    if arg is None:
        return "null"
    if is_number(arg):
        return "number"
    if isinstance(arg, str):
        return "string"
    if isinstance(arg, bool):
        return "boolean"
    if isinstance(arg, list):
        return "array"
    if isinstance(arg, dict):
        return "object"
    return MISSING


def fn_exists(arg: Any) -> bool:
    # Only MISSING means "doesn't exist" -- None (JSON null) is a real
    # value and $exists(null) is True (D1: never conflate null with MISSING).
    return arg is not MISSING


# =============================================================================
# Built-in functions -- numeric (thin delegations to runtime.numeric.builtins)
# =============================================================================


def fn_floor(arg: Any) -> Any:
    if arg is MISSING:
        return MISSING
    return math.floor(to_number(arg))


def fn_ceil(arg: Any) -> Any:
    if arg is MISSING:
        return MISSING
    return math.ceil(to_number(arg))


def fn_round(arg: Any, precision: Any = MISSING) -> Any:
    from .numeric import builtins as _numeric_builtins

    return _numeric_builtins.fn_round(arg, precision)


def fn_abs(arg: Any) -> Any:
    if arg is MISSING:
        return MISSING
    return num_node(abs(to_number(arg)))


def fn_sqrt(arg: Any) -> Any:
    if arg is MISSING:
        return MISSING
    v = to_number(arg)
    if v < 0:
        raise RuntimeEvaluationError("D3060", "$sqrt: the sqrt function cannot be applied to a negative number")
    return num_node(math.sqrt(v))


def fn_power(base: Any, exp: Any) -> Any:
    if base is MISSING or exp is MISSING:
        return MISSING
    try:
        result = math.pow(to_number(base), to_number(exp))
    except (ValueError, OverflowError):
        raise RuntimeEvaluationError(
            "D3061", "$power() function: the result of the power function is out of range"
        ) from None
    if math.isinf(result) or math.isnan(result):
        raise RuntimeEvaluationError("D3061", "$power() function: the result of the power function is out of range")
    return num_node(result)


def fn_random() -> Any:
    from .numeric import builtins as _numeric_builtins

    return _numeric_builtins.fn_random()


def fn_formatBase(number: Any, radix: Any = MISSING) -> Any:
    from .numeric import builtins as _numeric_builtins

    return _numeric_builtins.fn_formatBase(number, radix)


def fn_formatNumber(number: Any, picture: Any, options: Any = MISSING) -> Any:
    from .numeric import builtins as _numeric_builtins

    return _numeric_builtins.fn_formatNumber(number, picture, options)


def fn_formatInteger(number: Any, picture: Any) -> Any:
    from .numeric import builtins as _numeric_builtins

    return _numeric_builtins.fn_formatInteger(number, picture)


def fn_parseInteger(string: Any, picture: Any) -> Any:
    from .numeric import builtins as _numeric_builtins

    return _numeric_builtins.fn_parseInteger(string, picture)


# =============================================================================
# Built-in functions -- string (thin delegations to runtime.strings.builtins)
# =============================================================================


def _strings() -> ModuleType:
    from .strings import builtins as _string_builtins

    return _string_builtins


def fn_uppercase(arg: Any) -> Any:
    return _strings().fn_uppercase(arg)


def fn_lowercase(arg: Any) -> Any:
    return _strings().fn_lowercase(arg)


def fn_trim(arg: Any) -> Any:
    return _strings().fn_trim(arg)


def fn_length(arg: Any) -> Any:
    return _strings().fn_length(arg)


def fn_length_ctx(arg: Any) -> Any:
    return _strings().fn_length_ctx(arg)


def fn_substring(str_: Any, start: Any, length: Any = MISSING) -> Any:
    return _strings().fn_substring(str_, start, length)


def fn_substringBefore(str_: Any, chars: Any) -> Any:
    return _strings().fn_substringBefore(str_, chars)


def fn_substringBefore_ctx(str_: Any, chars: Any) -> Any:
    return _strings().fn_substringBefore_ctx(str_, chars)


def fn_substringAfter(str_: Any, chars: Any) -> Any:
    return _strings().fn_substringAfter(str_, chars)


def fn_substringAfter_ctx(str_: Any, chars: Any) -> Any:
    return _strings().fn_substringAfter_ctx(str_, chars)


def fn_contains(str_: Any, search: Any) -> Any:
    return _strings().fn_contains(str_, search)


def fn_split(str_: Any, separator: Any, limit: Any = MISSING) -> Any:
    return _strings().fn_split(str_, separator, limit)


def fn_match(str_: Any, pattern: Any, limit: Any = MISSING) -> Any:
    return _strings().fn_match(str_, pattern, limit)


def fn_replace(str_: Any, pattern: Any, replacement: Any, limit: Any = MISSING) -> Any:
    return _strings().fn_replace(str_, pattern, replacement, limit)


def fn_join(arr: Any, separator: Any = MISSING) -> Any:
    return _strings().fn_join(arr, separator)


def fn_pad(str_: Any, width: Any, pad_char: Any = MISSING) -> Any:
    return _strings().fn_pad(str_, width, pad_char)


def fn_eval(expr: Any, context: Any = MISSING) -> Any:
    return _strings().fn_eval(expr, context)


def fn_base64encode(str_: Any) -> Any:
    return _strings().fn_base64encode(str_)


def fn_base64decode(str_: Any) -> Any:
    return _strings().fn_base64decode(str_)


def fn_encodeUrlComponent(str_: Any) -> Any:
    return _strings().fn_encodeUrlComponent(str_)


def fn_decodeUrlComponent(str_: Any) -> Any:
    return _strings().fn_decodeUrlComponent(str_)


def fn_encodeUrl(str_: Any) -> Any:
    return _strings().fn_encodeUrl(str_)


def fn_decodeUrl(str_: Any) -> Any:
    return _strings().fn_decodeUrl(str_)


# =============================================================================
# Built-in functions -- array / sequence (aggregates live here; HOF live in
# sequences.py)
# =============================================================================


def fn_count(arg: Any) -> int:
    if arg is MISSING:
        return 0
    return len(arg) if isinstance(arg, list) else 1


def fn_count_field(seq: Any, field_name: str) -> int:
    """Fused $count(arr.field): counts elements in all field values
    without materializing the field array."""
    if seq is MISSING:
        return 0
    if not isinstance(seq, list):
        if not isinstance(seq, dict):
            return 0
        v = seq.get(field_name, MISSING)
        if v is MISSING:
            return 0
        return len(v) if isinstance(v, list) else 1
    count = 0
    for elem in seq:
        if not isinstance(elem, dict):
            continue
        v = elem.get(field_name, MISSING)
        if v is MISSING:
            continue
        count += len(v) if isinstance(v, list) else 1
    return count


def fn_count_field_eq(seq: Any, field_name: str, expected: Any) -> int:
    """Fused $count(seq[field = value])."""
    if seq is None or seq is MISSING:
        return 0

    def matches(v: Any) -> bool:
        return _deep_equals(v, expected)

    if isinstance(expected, str):
        matches = lambda v: isinstance(v, str) and v == expected  # noqa: E731
    elif is_number(expected):
        matches = lambda v: is_number(v) and float(v) == float(expected)  # noqa: E731
    elif isinstance(expected, bool):
        matches = lambda v: isinstance(v, bool) and v == expected  # noqa: E731

    return _count_matching(seq, field_name, matches)


def _count_matching(seq: Any, field_name: str, matches: Callable[[Any], bool]) -> int:
    if not isinstance(seq, list):
        return 1 if _field_matches(seq, field_name, matches) else 0
    return sum(1 for elem in seq if _field_matches(elem, field_name, matches))


def _field_matches(node: Any, field_name: str, matches: Callable[[Any], bool]) -> bool:
    if not isinstance(node, dict):
        return False
    value = node.get(field_name, MISSING)
    return value is not MISSING and matches(value)


def fn_count_filter(seq: Any, predicate: Callable[[Any], Any]) -> int:
    if seq is MISSING:
        return 0
    if not isinstance(seq, list):
        return 1 if is_truthy(predicate(seq)) else 0
    return sum(1 for elem in seq if is_truthy(predicate(elem)))


def _require_t0412(n: Any, fn_name: str) -> None:
    t = n.__class__
    if t is int or t is float:
        return
    if not is_number(n):
        raise RuntimeEvaluationError("T0412", f"{fn_name} requires an array of numbers, but found {_kind(n)}")


def _require_average_arg(n: Any) -> None:
    t = n.__class__
    if t is int or t is float:
        return
    if not is_number(n):
        raise RuntimeEvaluationError("T0412", f"$average requires an array of numbers, but found {_kind(n)}")


def _iter_or_single(v: Any) -> list[Any]:
    return v if isinstance(v, list) else [v]


def fn_sum_field(seq: Any, field_name: str, field_name2: str | None = None) -> Any:
    """Fused $sum(arr.field) / $sum(arr.f1.f2)."""
    if seq is MISSING:
        return MISSING
    total = 0.0
    any_ = False
    # The shape (list vs scalar) is known at each level here, so a local
    # branch avoids the _iter_or_single() call + one-element-list alloc
    # that would otherwise happen for every non-list value in the loop.
    for elem in seq if isinstance(seq, list) else (seq,):
        if not isinstance(elem, dict):
            continue
        v1 = elem.get(field_name, MISSING)
        if v1 is MISSING:
            continue
        if field_name2 is not None:
            for sub in v1 if isinstance(v1, list) else (v1,):
                if not isinstance(sub, dict):
                    continue
                v2 = sub.get(field_name2, MISSING)
                if v2 is MISSING:
                    continue
                for n in v2 if isinstance(v2, list) else (v2,):
                    _require_t0412(n, "$sum")
                    total += float(n)
                    any_ = True
        else:
            for n in v1 if isinstance(v1, list) else (v1,):
                _require_t0412(n, "$sum")
                total += float(n)
                any_ = True
    return num_node(total) if any_ else MISSING


def fn_average_field(seq: Any, field_name: str, field_name2: str | None = None) -> Any:
    if seq is MISSING:
        return MISSING
    total = 0.0
    count = 0
    for elem in seq if isinstance(seq, list) else (seq,):
        if not isinstance(elem, dict):
            continue
        v1 = elem.get(field_name, MISSING)
        if v1 is MISSING:
            continue
        if field_name2 is not None:
            for sub in v1 if isinstance(v1, list) else (v1,):
                if not isinstance(sub, dict):
                    continue
                v2 = sub.get(field_name2, MISSING)
                if v2 is MISSING:
                    continue
                for n in v2 if isinstance(v2, list) else (v2,):
                    _require_average_arg(n)
                    total += float(n)
                    count += 1
        else:
            for n in v1 if isinstance(v1, list) else (v1,):
                _require_average_arg(n)
                total += float(n)
                count += 1
    return MISSING if count == 0 else num_node(total / count)


def fn_max_field(seq: Any, field_name: str, field_name2: str | None = None) -> Any:
    if seq is MISSING:
        return MISSING
    best = -math.inf
    any_ = False
    for elem in seq if isinstance(seq, list) else (seq,):
        if not isinstance(elem, dict):
            continue
        v1 = elem.get(field_name, MISSING)
        if v1 is MISSING:
            continue
        if field_name2 is not None:
            for sub in v1 if isinstance(v1, list) else (v1,):
                if not isinstance(sub, dict):
                    continue
                v2 = sub.get(field_name2, MISSING)
                if v2 is MISSING:
                    continue
                for n in v2 if isinstance(v2, list) else (v2,):
                    _require_t0412(n, "$max")
                    d = float(n)
                    if d > best:
                        best = d
                    any_ = True
        else:
            for n in v1 if isinstance(v1, list) else (v1,):
                _require_t0412(n, "$max")
                d = float(n)
                if d > best:
                    best = d
                any_ = True
    return num_node(best) if any_ else MISSING


def fn_min_field(seq: Any, field_name: str, field_name2: str | None = None) -> Any:
    if seq is MISSING:
        return MISSING
    best = math.inf
    any_ = False
    for elem in seq if isinstance(seq, list) else (seq,):
        if not isinstance(elem, dict):
            continue
        v1 = elem.get(field_name, MISSING)
        if v1 is MISSING:
            continue
        if field_name2 is not None:
            for sub in v1 if isinstance(v1, list) else (v1,):
                if not isinstance(sub, dict):
                    continue
                v2 = sub.get(field_name2, MISSING)
                if v2 is MISSING:
                    continue
                for n in v2 if isinstance(v2, list) else (v2,):
                    _require_t0412(n, "$min")
                    d = float(n)
                    if d < best:
                        best = d
                    any_ = True
        else:
            for n in v1 if isinstance(v1, list) else (v1,):
                _require_t0412(n, "$min")
                d = float(n)
                if d < best:
                    best = d
                any_ = True
    return num_node(best) if any_ else MISSING


def fn_sum(arg: Any) -> Any:
    if arg is MISSING:
        return MISSING
    if not isinstance(arg, list):
        _require_t0412(arg, "$sum")
        return num_node(float(arg))
    if len(arg) == 0:
        return 0
    total = 0.0
    for elem in arg:
        _require_t0412(elem, "$sum")
        total += float(elem)
    return num_node(total)


def fn_max(arg: Any) -> Any:
    if arg is MISSING:
        return MISSING
    if not isinstance(arg, list):
        _require_t0412(arg, "$max")
        return num_node(float(arg))
    if len(arg) == 0:
        return MISSING
    best = -math.inf
    for elem in arg:
        _require_t0412(elem, "$max")
        v = float(elem)
        if v > best:
            best = v
    return num_node(best)


def fn_min(arg: Any) -> Any:
    if arg is MISSING:
        return MISSING
    if not isinstance(arg, list):
        _require_t0412(arg, "$min")
        return num_node(float(arg))
    if len(arg) == 0:
        return MISSING
    best = math.inf
    for elem in arg:
        _require_t0412(elem, "$min")
        v = float(elem)
        if v < best:
            best = v
    return num_node(best)


def fn_average(arg: Any) -> Any:
    if arg is MISSING:
        return MISSING
    if not isinstance(arg, list):
        _require_t0412(arg, "$average")
        return num_node(float(arg))
    if len(arg) == 0:
        return MISSING
    total = 0.0
    for elem in arg:
        _require_average_arg(elem)
        total += float(elem)
    return num_node(total / len(arg))


def fn_append(a: Any, b: Any) -> Any:
    if a is MISSING:
        return b
    if b is MISSING:
        return a
    result: list[Any] = []
    _append_to_sequence(result, a)
    _append_to_sequence(result, b)
    return result


def fn_reverse(arg: Any) -> Any:
    if arg is MISSING:
        return MISSING
    if not isinstance(arg, list):
        return arg
    return list(reversed(arg))


def fn_distinct(arg: Any) -> Any:
    if arg is MISSING:
        return MISSING
    if not isinstance(arg, list):
        return arg
    result: list[Any] = []
    for elem in arg:
        if not any(_deep_equals(elem, seen) for seen in result):
            result.append(elem)
    return result


def fn_flatten(arg: Any) -> Any:
    if arg is MISSING:
        return MISSING
    result: list[Any] = []
    _flatten_into(arg, result)
    return result


def _flatten_into(node: Any, acc: list[Any]) -> None:
    if isinstance(node, list):
        for e in node:
            _flatten_into(e, acc)
    elif node is not MISSING:
        acc.append(node)


def fn_shuffle(arg: Any) -> Any:
    from .sequences import fn_shuffle as _fn_shuffle

    return _fn_shuffle(arg)


def fn_zip(*arrays: Any) -> Any:
    if not arrays:
        return MISSING
    normalised: list[list[Any]] = []
    min_len: int | None = None
    for arr in arrays:
        if arr is MISSING:
            norm: list[Any] = []
        elif not isinstance(arr, list):
            norm = [arr]
        else:
            norm = arr
        normalised.append(norm)
        min_len = len(norm) if min_len is None else min(min_len, len(norm))
    min_len = min_len or 0
    result = []
    for i in range(min_len):
        result.append([arr[i] for arr in normalised])
    return result


# =============================================================================
# Higher-order sequence built-ins (thin delegations to sequences.py)
# =============================================================================


def deadline_guard(fn: Callable[[Any], Any]) -> Callable[[Any], Any]:
    """Wraps a callback so that a long-running loop over it observes
    set_timeout(). Only installed when a deadline is actually active."""
    if not _ctx.has_deadline():
        return fn
    calls = [0]

    def guarded(element: Any) -> Any:
        calls[0] += 1
        if (calls[0] & 0x3F) == 0:
            _ctx.check_timeout()
        return fn(element)

    return guarded


def element_callback(fn: Any) -> Callable[[Any], Any]:
    """Adapts a function value to a callback that receives the element
    alone."""
    from .lambdas import fn_apply

    return lambda elem: fn_apply(fn, elem)


def tuple_callback(fn: Any) -> Callable[[Any], Any]:
    """Adapts a function value to a callback that receives a tuple when
    the function declares two or more parameters, its first slot
    otherwise."""
    from .lambdas import fn_apply, lambda_arity

    if lambda_arity(fn) >= 2:
        return lambda t: fn_apply(fn, t)
    return lambda t: fn_apply(fn, t[0])


def fn_map(arr: Any, fn: Any) -> Any:
    from . import sequences as _seq
    from .lambdas import lambda_arity

    if not isinstance(fn, JLambda):
        # A literal/generated callback (plain Python callable) -- the
        # translator already picked the right call site by arity.
        return _seq.fn_map(arr, fn)
    if lambda_arity(fn) >= 2:
        return _seq.fn_map_indexed(arr, tuple_callback(fn))
    return _seq.fn_map(arr, element_callback(fn))


def fn_filter(arr: Any, predicate: Any) -> Any:
    from . import sequences as _seq
    from .lambdas import lambda_arity

    if not isinstance(predicate, JLambda):
        return _seq.fn_filter(arr, predicate)
    if lambda_arity(predicate) >= 2:
        return _seq.fn_filter_indexed(arr, tuple_callback(predicate))
    return _seq.fn_filter(arr, element_callback(predicate))


def fn_single(arr: Any, predicate: Any = MISSING) -> Any:
    from . import sequences as _seq
    from .lambdas import lambda_arity

    if predicate is MISSING:
        return _seq.fn_single_one_arg(arr)
    if not isinstance(predicate, JLambda):
        return _seq.fn_single(arr, predicate)
    if lambda_arity(predicate) >= 2:
        return _seq.fn_single_indexed(arr, tuple_callback(predicate))
    return _seq.fn_single(arr, element_callback(predicate))


def fn_sift(obj: Any, fn: Any) -> Any:
    from . import sequences as _seq

    if not isinstance(fn, JLambda):
        return _seq.fn_sift(obj, fn)
    return _seq.fn_sift(obj, tuple_callback(fn))


def fn_each(obj: Any, fn: Any) -> Any:
    from . import sequences as _seq

    if not isinstance(fn, JLambda):
        return _seq.fn_each(obj, fn)
    return _seq.fn_each(obj, tuple_callback(fn))


def fn_sort(arr: Any, fn: Any = MISSING) -> Any:
    from . import sequences as _seq
    from .lambdas import lambda_arity

    if fn is MISSING:
        return _seq.fn_sort(arr, None)
    if not isinstance(fn, JLambda):
        return _seq.fn_sort(arr, fn)
    if lambda_arity(fn) >= 2:
        return _seq.fn_sort_comparator(arr, tuple_callback(fn))
    return _seq.fn_sort(arr, element_callback(fn))


def fn_sort_comparator(arr: Any, comparator_fn: Callable[[Any], Any]) -> Any:
    from . import sequences as _seq

    return _seq.fn_sort_comparator(arr, comparator_fn)


def fn_sort_by_ordering_key(arr: Any, key_fn: Callable[[Any], Any], descending: bool) -> Any:
    from . import sequences as _seq

    return _seq.fn_sort_by_ordering_key(arr, key_fn, descending)


def fn_reduce(arr: Any, fn: Callable[[Any], Any], init: Any = MISSING) -> Any:
    from . import sequences as _seq

    return _seq.fn_reduce(arr, fn, init)


def fn_map_indexed(arr: Any, fn: Callable[[Any], Any]) -> Any:
    from . import sequences as _seq

    return _seq.fn_map_indexed(arr, fn)


def fn_filter_indexed(arr: Any, predicate: Callable[[Any], Any]) -> Any:
    from . import sequences as _seq

    return _seq.fn_filter_indexed(arr, predicate)


def fn_single_indexed(arr: Any, predicate: Callable[[Any], Any]) -> Any:
    from . import sequences as _seq

    return _seq.fn_single_indexed(arr, predicate)


def fn_collect_pairs(source: Any, elem_fn: Callable[[Any], Any]) -> Any:
    from . import sequences as _seq

    return _seq.fn_collect_pairs(source, elem_fn)


def fn_collect_triples(grandparents: Any, parent_fn: Callable[[Any], Any], elem_fn: Callable[[Any], Any]) -> Any:
    from . import sequences as _seq

    return _seq.fn_collect_triples(grandparents, parent_fn, elem_fn)


def each_indexed(seq: Any, fn: Callable[[Any], Any]) -> Any:
    """Maps fn over each element of seq, passing [element, index]. Used by
    the positional-binding operator (#$i)."""
    if seq is None or seq is MISSING:
        return MISSING
    result: list[Any] = []
    if isinstance(seq, list):
        for i, elem in enumerate(seq):
            val = fn([elem, i])
            if val is not MISSING:
                _append_to_sequence(result, val)
    else:
        val = fn([seq, 0])
        if val is not MISSING:
            _append_to_sequence(result, val)
    return _unwrap_list(result)


def merge_group_by_objects(seq: Any) -> Any:
    """Merges a list of dict results (one per binding-loop iteration) into
    a single dict. Duplicate keys accumulate into lists."""
    if seq is None or seq is MISSING:
        return MISSING
    if not isinstance(seq, list):
        return seq if isinstance(seq, dict) else MISSING
    result: dict[str, Any] = {}
    for item in seq:
        if not isinstance(item, dict):
            continue
        for k, v in item.items():
            if k not in result:
                result[k] = v
            else:
                # Never mutate `existing` in place: the first value stored for a
                # key is frequently a list navigated straight out of the caller's
                # input document (field() on a dict returns the aliased original),
                # so extend()/append() here would corrupt the input. Always build
                # a fresh list instead.
                existing = result[k]
                head = existing if isinstance(existing, list) else [existing]
                result[k] = [*head, *v] if isinstance(v, list) else [*head, v]
    return result if result else MISSING


# =============================================================================
# Built-in functions -- object
# =============================================================================


def fn_keys(obj: Any) -> Any:
    if obj is MISSING:
        return MISSING
    if isinstance(obj, list):
        seen: dict[str, None] = {}
        for elem in obj:
            if isinstance(elem, dict):
                for k in elem:
                    seen[k] = None
        if not seen:
            return MISSING
        return _unwrap_list(list(seen.keys()))
    if not isinstance(obj, dict):
        return MISSING
    keys = list(obj.keys())
    return MISSING if not keys else _unwrap_list(keys)


def fn_values(obj: Any) -> Any:
    if obj is MISSING or not isinstance(obj, dict):
        return MISSING
    values = list(obj.values())
    return values if values else MISSING


def fn_transform(source: Any, location_fn: Callable[[Any], Any], update_fn: Callable[[Any], Any], delete_fields: Any) -> Any:
    """Implements the transform operator src ~> |location|update[,delete]|."""
    if source is MISSING:
        return MISSING
    copy = _deep_copy(source)
    targets = location_fn(copy)
    if targets is not MISSING:
        target_list = targets if isinstance(targets, list) else [targets]
        for target in target_list:
            if not isinstance(target, dict):
                continue
            update = update_fn(target)
            if update is not MISSING:
                if not isinstance(update, dict):
                    raise RuntimeEvaluationError(
                        "T2011",
                        "The update clause of the transform operator requires an object literal as the second operand",
                    )
                target.update(update)
            if delete_fields is not MISSING:
                if not isinstance(delete_fields, str) and not isinstance(delete_fields, list):
                    raise RuntimeEvaluationError(
                        "T2012",
                        "The delete clause of the transform operator is not valid, must be a string or array of strings",
                    )
                if isinstance(delete_fields, str):
                    target.pop(delete_fields, None)
                else:
                    for f in delete_fields:
                        if isinstance(f, str):
                            target.pop(f, None)
    return copy


def _deep_copy(v: Any) -> Any:
    if isinstance(v, dict):
        return {k: _deep_copy(x) for k, x in v.items()}
    if isinstance(v, list):
        return [_deep_copy(x) for x in v]
    return v


def fn_merge(arr: Any) -> Any:
    if arr is MISSING:
        return MISSING
    result: dict[str, Any] = {}
    for item in _iter_or_single(arr):
        if isinstance(item, dict):
            result.update(item)
    return result


def fn_lookup(obj: Any, key: Any) -> Any:
    if obj is MISSING or key is MISSING:
        return MISSING
    if not isinstance(key, str):
        return MISSING
    if isinstance(obj, dict):
        return obj.get(key, MISSING)
    if isinstance(obj, list):
        result: list[Any] = []
        for elem in obj:
            if isinstance(elem, dict):
                v = elem.get(key, MISSING)
                if v is not MISSING:
                    _append_to_sequence(result, v)
        return _unwrap_list(result)
    return MISSING


def fn_spread(obj: Any) -> Any:
    if obj is MISSING:
        return MISSING
    if isinstance(obj, list):
        result: list[Any] = []
        for elem in obj:
            spread = fn_spread(elem)
            if spread is not MISSING:
                _append_to_sequence(result, spread)
        return _unwrap_list(result)
    if not isinstance(obj, dict):
        return obj
    return [{k: v} for k, v in obj.items()]


def fn_assert(condition: Any, message: Any) -> Any:
    if condition is not MISSING and not isinstance(condition, bool):
        raise RuntimeEvaluationError("T0410", "Argument 1 of function $assert must be a boolean")
    if not is_truthy(condition):
        m = "$assert() statement failed" if message is MISSING else to_text(message)
        raise RuntimeEvaluationError("D3141", m)
    return MISSING


def fn_error(msg: Any = MISSING) -> Any:
    if msg is not MISSING and not isinstance(msg, str):
        raise RuntimeEvaluationError("T0410", "$error: argument must be a string")
    m = msg if isinstance(msg, str) else "$error() function evaluated"
    raise RuntimeEvaluationError("D3137", m)


# =============================================================================
# Date/time (thin delegations to runtime.datetime)
# =============================================================================


def fn_now(picture: Any = MISSING, timezone: Any = MISSING) -> Any:
    from .datetime import iso as _iso

    if picture is MISSING:
        return _iso.millis_to_iso(_ctx.evaluation_millis())
    tz = None if timezone is MISSING else to_text(timezone)
    return _iso.millis_to_picture(_ctx.evaluation_millis(), to_text(picture), tz)


def fn_millis() -> Any:
    return _ctx.evaluation_millis()


def fn_fromMillis(millis: Any, picture: Any = MISSING, timezone: Any = MISSING) -> Any:
    from .datetime import iso as _iso

    if millis is MISSING:
        return MISSING
    if _missing_or_empty(picture) and _missing_or_empty(timezone):
        return _iso.millis_to_iso(int(to_number(millis)))
    if _missing_or_empty(picture):
        tz = None if _missing_or_empty(timezone) else to_text(timezone)
        return _iso.millis_to_iso(int(to_number(millis)), tz)
    tz = None if _missing_or_empty(timezone) else to_text(timezone)
    return _iso.millis_to_picture(int(to_number(millis)), to_text(picture), tz)


def fn_toMillis(timestamp: Any, picture: Any = MISSING) -> Any:
    from .datetime import iso as _iso

    if timestamp is MISSING:
        return MISSING
    if picture is MISSING:
        return _iso.iso_to_millis(to_text(timestamp))
    result = _iso.picture_to_millis(to_text(timestamp), to_text(picture))
    return MISSING if result is None else result


def _missing_or_empty(n: Any) -> bool:
    return n is None or n is MISSING or (isinstance(n, str) and n == "")


# =============================================================================
# Chain operator / lambda / regex (thin delegations)
# =============================================================================


def fn_pipe(arg: Any, fn: Any) -> Any:
    from .lambdas import fn_pipe as _fn_pipe

    return _fn_pipe(arg, fn)


def fn_apply(fn: Any, arg: Any) -> Any:
    from .lambdas import fn_apply as _fn_apply

    return _fn_apply(fn, arg)


def fn_apply_tco(fn: Any, arg: Any) -> Any:
    from .lambdas import fn_apply_tco as _fn_apply_tco

    return _fn_apply_tco(fn, arg)


def lambda_value(fn: Callable[[Any], Any], arity: int = -1) -> JLambda:
    return JLambda(fn, arity)


def lambda_arity(fn: Any) -> int:
    from .lambdas import lambda_arity as _lambda_arity

    return _lambda_arity(fn)


def regex_value(pattern: str, flags: str) -> Any:
    from .strings import regex_ops as _regex_ops

    return _regex_ops.regex_value(pattern, flags)


# =============================================================================
# Bindings support (thin delegations to context.py)
# =============================================================================


def resolve_binding(name: str) -> Any:
    return _ctx.resolve_binding(name)


def call_bound_function(name: str, args: list[Any]) -> Any:
    return _ctx.call_bound_function(name, args)


def begin_evaluation(permanent: Any, per_eval: Any, timeout_ms: int, eval_delegate: Any = None) -> None:
    _ctx.begin_evaluation(permanent, per_eval, timeout_ms, eval_delegate)


def end_evaluation() -> None:
    _ctx.end_evaluation()


def is_evaluation_active() -> bool:
    return _ctx.is_active()


# =============================================================================
# Internal helpers
# =============================================================================


def discard(ignored: Any) -> None:
    return None


def next_counter(box: list[int]) -> int:
    """Post-increment as a pure expression: box[0]++ in Java becomes
    next_counter(box) here, since a Python lambda body cannot contain the
    statement box[0] += 1. Used by the translator for the rare #$pos
    pattern that needs a globally-incrementing counter across outer
    binding loops."""
    v = box[0]
    box[0] = v + 1
    return v


def is_lambda_token(node: Any) -> bool:
    return is_function(node)


def is_regex_token(node: Any) -> bool:
    return is_regex(node)


def to_number(n: Any) -> float:
    """Coerces n to a float. Coerces numeric string representations;
    raises for other types."""
    if is_number(n):
        return float(n)
    if isinstance(n, str):
        try:
            return float(n)
        except ValueError:
            raise RuntimeEvaluationError("D3020", f"Cannot coerce string to number: {n}") from None
    if isinstance(n, bool):
        return 1.0 if n else 0.0
    raise RuntimeEvaluationError("D3020", f"Cannot coerce {_kind(n)} to number")


def to_text(n: Any) -> str:
    """Converts n to a string representation.

    Exact-type first: an int renders as str(n) directly, where the
    general path would send it through float() -> number_to_string() ->
    math.floor() -> str(int()). bool is an int *subclass*, so
    True.__class__ is bool and it correctly falls through to
    _to_text_general, which still renders "true"/"false".
    """
    t = n.__class__
    if t is str:
        return cast(str, n)  # `type(n) is str` is exact; mypy cannot narrow on it
    if t is int:
        return str(n)
    if t is float:
        return number_to_string(n)
    return _to_text_general(n)


def _to_text_general(n: Any) -> str:
    if isinstance(n, str):
        return n
    if isinstance(n, bool):
        return "true" if n else "false"
    if is_number(n):
        return number_to_string(float(n))
    if n is None:
        return "null"
    if is_function(n) or is_regex(n):
        return ""
    if isinstance(n, (list, dict)):
        return json_encode_compact(n)
    return str(n)


def json_encode_compact(v: Any) -> str:
    """Compact JSON serialisation using JSONata's number-to-string rules
    for numbers (not Python's/json's own float formatting)."""
    import json as _json

    if isinstance(v, str):
        return _json.dumps(v, ensure_ascii=False)
    if isinstance(v, bool):
        return "true" if v else "false"
    if is_number(v):
        return number_to_string(float(v))
    if v is None:
        return "null"
    if is_function(v) or is_regex(v):
        return '""'
    if isinstance(v, list):
        return "[" + ",".join(json_encode_compact(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ",".join(f"{_json.dumps(k, ensure_ascii=False)}:{json_encode_compact(x)}" for k, x in v.items()) + "}"
    return _json.dumps(str(v), ensure_ascii=False)


def json_encode_pretty(v: Any, level: int = 0) -> str:
    """2-space-indented JSON serialisation, matching Jackson's
    DefaultPrettyPrinter output shape ("key": value, Unix newlines, empty
    containers collapsed to []/{})."""
    import json as _json

    pad = "  " * level
    pad_inner = "  " * (level + 1)
    if isinstance(v, list):
        if not v:
            return "[]"
        items = [pad_inner + json_encode_pretty(x, level + 1) for x in v]
        return "[\n" + ",\n".join(items) + "\n" + pad + "]"
    if isinstance(v, dict):
        if not v:
            return "{}"
        items = [
            f"{pad_inner}{_json.dumps(k, ensure_ascii=False)}: {json_encode_pretty(x, level + 1)}"
            for k, x in v.items()
        ]
        return "{\n" + ",\n".join(items) + "\n" + pad + "}"
    return json_encode_compact(v)


def sanitize_for_string(n: Any) -> Any:
    """Returns n with function and regex values replaced by empty
    strings."""
    if is_function(n) or is_regex(n):
        return ""
    if not _contains_function_value(n):
        return n
    if isinstance(n, list):
        return [sanitize_for_string(x) for x in n]
    if isinstance(n, dict):
        return {k: sanitize_for_string(v) for k, v in n.items()}
    return n


def _contains_function_value(n: Any) -> bool:
    if is_function(n) or is_regex(n):
        return True
    if isinstance(n, list):
        return any(_contains_function_value(x) for x in n)
    if isinstance(n, dict):
        return any(_contains_function_value(v) for v in n.values())
    return False


def _is_plain_within_15_significant_digits(s: str) -> bool:
    """True if s (always a float repr) is plain notation carrying no more
    than 15 significant digits.

    Hot: every $string/& of a fractional number reaches here. A repr with
    no exponent and at most 15 characters cannot hold more than 15
    significant digits -- '-' and '.' only take space away from digits --
    so the common case is decided by len() alone, and the rest counts
    digits with C-level string methods rather than a per-character loop.
    """
    if len(s) <= 15:
        if "e" in s or "E" in s:
            return False
    else:
        if "e" in s or "E" in s:
            return False
        # Significant digits = every digit from the first non-zero on,
        # trailing zeros included.
        if len(s.replace("-", "").replace(".", "").lstrip("0")) > 15:
            return False
    return not s.startswith("0.0000") and not s.startswith("-0.0000")


def number_to_string(v: float) -> str:
    """Converts a float to string using JSONata's (JavaScript-compatible)
    number-to-string rules: integers render without a decimal point,
    fractional values use at most 15 significant digits, trailing zeros are
    stripped, and exponential notation uses a lowercase e."""
    if math.isinf(v) or math.isnan(v):
        return str(v)
    if v == math.floor(v) and not math.isinf(v):
        if abs(v) < 1e15:
            return str(int(v))
        if abs(v) < 1e21:
            return str(int(v))
        ctx = Context(prec=15, rounding=ROUND_HALF_UP)
        bd = ctx.create_decimal(Decimal(v))
        s = format(bd, "E").replace("E", "e")
        s = _strip_trailing_zeros_before_e(s)
        return _ensure_exponent_sign(s)
    # JS Number::toString switches to exponential notation only below
    # 1e-6 in magnitude (1e-6 itself still prints plain: "0.000001";
    # 9.9e-7 prints "9.9e-7"). A numeric threshold, not a string-prefix
    # heuristic, is what actually matches that boundary.
    use_exponential = v != 0 and abs(v) < 1e-6
    shortest = repr(v)
    if not use_exponential and _is_plain_within_15_significant_digits(shortest):
        return shortest
    ctx = Context(prec=15, rounding=ROUND_HALF_UP)
    bd = ctx.create_decimal(Decimal(v)).normalize()
    if use_exponential:
        s = format(bd, "E").replace("E", "e")
        s = _strip_trailing_zeros_before_e(s)
        s = _ensure_exponent_sign(s)
    else:
        s = format(bd, "f")
    return s


def _strip_trailing_zeros_before_e(s: str) -> str:
    if "e" not in s:
        return s
    mantissa, _, exp = s.partition("e")
    if "." in mantissa:
        mantissa = mantissa.rstrip("0").rstrip(".")
    return f"{mantissa}e{exp}"


def _ensure_exponent_sign(s: str) -> str:
    if "e" not in s:
        return s
    mantissa, _, exp = s.partition("e")
    if exp and exp[0] not in "+-":
        exp = f"+{exp}"
    return f"{mantissa}e{exp}"


def clamp_index(i: int, length: int) -> int:
    if i < 0:
        i = max(0, length + i)
    return min(i, length)


def _append_to_sequence(acc: list[Any], val: Any) -> None:
    """Adds val to acc, flattening one level of lists (JSONata sequence
    flattening rule)."""
    if isinstance(val, list):
        acc.extend(val)
    elif val is not MISSING:
        acc.append(val)


def _unwrap_list(arr: list[Any]) -> Any:
    """Returns the single element if arr has exactly one item, else arr
    as-is. Empty lists return MISSING."""
    if len(arr) == 0:
        return MISSING
    if len(arr) == 1:
        return arr[0]
    return arr


def unwrap(node: Any) -> Any:
    if node is None or node is MISSING:
        return MISSING
    if not isinstance(node, list):
        return node
    return _unwrap_list(node)
