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

import json as _json_module
import math
from collections.abc import Callable
from functools import lru_cache
from types import ModuleType
from typing import Any, cast

from ..errors import _RuntimeEvaluationError as RuntimeEvaluationError
from . import context as _ctx
from .context import DEADLINE_STACK as _DEADLINE_STACK
from .values import MISSING, JLambda, JRegex, Preserved, is_function, is_number, is_regex

__all__ = [
    "MISSING",
    "NULL",
    "RESCAN",
    "JLambda",
    "JRegex",
    "Preserved",
    "RangeHolder",
    # arithmetic
    # array / sequence builtins
    # bindings support (thin delegations to context.py)
    # chain / lambda / regex
    # constructors
    # date/time
    # factory / path navigation
    # internal helpers referenced by generated code
    # object builtins
    # string builtins
    # string concat / comparisons / boolean logic
    # type coercion / scalar builtins
    # value model re-exports
    "add",
    "and_",
    "apply_step",
    "array",
    "array_of",
    "begin_evaluation",
    "call_bound_function",
    "call_field_step",
    "clamp_index",
    "coalesce",
    "collect_pos_tuples",
    "concat",
    "consarray_head",
    "constructor_step_final",
    "constructor_step_keep_singleton",
    "context_step",
    "deadline_guard",
    "deep_equals",
    "descendant",
    "discard",
    "div_d",
    "divide",
    "dynamic_filter",
    "each_indexed",
    "element_callback",
    "elvis",
    "end_evaluation",
    "eq",
    "field",
    "field_function",
    "filter_",
    "filter_field_eq",
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
    "fn_clone",
    "fn_collect_pairs",
    "fn_collect_triples",
    "fn_contains",
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
    "fn_now",
    "fn_number",
    "fn_pad",
    "fn_parseInteger",
    "fn_pipe",
    "fn_power",
    "fn_random",
    "fn_reduce",
    "fn_reduce_dynamic",
    "fn_replace",
    "fn_reverse",
    "fn_round",
    "fn_shuffle",
    "fn_sift",
    "fn_signature_error",
    "fn_single",
    "fn_single_indexed",
    "fn_sort",
    "fn_sort_by_ordering_key",
    "fn_sort_comparator",
    "fn_split",
    "fn_spread",
    "fn_sqrt",
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
    "fn_uppercase",
    "fn_values",
    "fn_zip",
    "force_array",
    "force_array_cons",
    "ge",
    "group_context",
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
    "keep_singleton",
    "lambda_arity",
    "lambda_value",
    "le",
    "lt",
    "map_consarray_step",
    "map_constructor_step",
    "map_constructor_step_flat",
    "map_group_step",
    "map_step",
    "matches_index_or_truthy",
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
    "object_of_distinct",
    "or_",
    "pack_args",
    "preserve_array",
    "range_",
    "range_flatten",
    "range_subscript",
    "regex_value",
    "require_function",
    "resolve_binding",
    "sanitize_for_string",
    "staged_consarray_head",
    "step_final",
    "subscript",
    "subtract",
    "to_number",
    "to_text",
    "tuple2",
    "tuple_callback",
    "unwrap",
    "unwrap_cons",
    "wildcard",
    "wildcard_context",
]

# JSONata "null" literal is Python None; MISSING is the "undefined" sentinel.
NULL = None


# =============================================================================
# Lazily-resolved sibling modules
#
# core.py is the facade the generated code imports, and it delegates a
# large part of the built-in surface to sibling modules. Those imports
# cannot move to module scope: core.py <-> sequences/lambdas/numeric/
# strings/datetime is circular, and keeping the heavy picture-string and
# regex machinery out of the import graph until something needs it is
# deliberate (a plain `import jsonata2py` should not pay for $formatNumber).
#
# But the `from .x import y` statements these replace sat *inside the
# delegating functions*, so every $round, $sort, $map, $string and
# $fromMillis call re-executed IMPORT_NAME: a relative-import resolution
# plus _handle_fromlist plus a getattr, measured at 425 ns per call --
# 64% of the total cost of a $round. Resolving once into a module global
# keeps the laziness and pays it exactly once per process.
# =============================================================================

_seq_mod: ModuleType | None = None
_lambdas_mod: ModuleType | None = None
_numeric_mod: ModuleType | None = None
_strings_mod: ModuleType | None = None
_regex_ops_mod: ModuleType | None = None
_iso_mod: ModuleType | None = None


def _sequences() -> ModuleType:
    global _seq_mod
    mod = _seq_mod
    if mod is None:
        from . import sequences as seq_mod

        mod = _seq_mod = seq_mod
    return mod


def _lambdas() -> ModuleType:
    global _lambdas_mod
    mod = _lambdas_mod
    if mod is None:
        from . import lambdas as lam_mod

        mod = _lambdas_mod = lam_mod
    return mod


def _numeric() -> ModuleType:
    global _numeric_mod
    mod = _numeric_mod
    if mod is None:
        from .numeric import builtins as numeric_builtins

        mod = _numeric_mod = numeric_builtins
    return mod


def _regex_ops_module() -> ModuleType:
    global _regex_ops_mod
    mod = _regex_ops_mod
    if mod is None:
        from .strings import regex_ops as regex_ops_mod

        mod = _regex_ops_mod = regex_ops_mod
    return mod


def _iso_module() -> ModuleType:
    global _iso_mod
    mod = _iso_mod
    if mod is None:
        from .datetime import iso as iso_mod

        mod = _iso_mod = iso_mod
    return mod


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
    """The `*` step applied to a context SEQUENCE (any step but a path's
    leading one).

    The reference's evaluateStep runs the step against each member of the
    context sequence, DISCARDS members whose result normalizes to
    undefined, and concatenates what is left -- so a scalar member
    contributes nothing, because only objects and arrays have keys.

    When exactly one member survives, evaluateStep hands its result back
    untouched instead of re-flattening it, which is how wildcard_context's
    unnormalized plain array leaks through a path step.
    """
    if node is None or node is MISSING:
        return MISSING
    if not isinstance(node, list):
        return wildcard_context(node)
    first: Any = MISSING
    result: list[Any] | None = None
    for elem in node:
        r = wildcard_context(elem)
        if r is MISSING:
            continue
        if result is not None:
            _append_to_sequence(result, r)
        elif first is MISSING:
            first = r
        else:
            result = []
            _append_to_sequence(result, first)
            _append_to_sequence(result, r)
            first = MISSING
    if result is not None:
        return _unwrap_list(result)
    return first


def wildcard_context(node: Any) -> Any:
    """The reference's evaluateWildcard: enumerate one node's keys (object
    properties, or array indices) and collect the values, deep-flattening
    every array-valued one.

    This is also `*` as a path's LEADING step, because the reference wraps
    an array input in a singleton sequence before evaluating a path -- the
    array ITSELF, not its members, is the node whose keys get enumerated.
    `*` over [1,2,3] is therefore [1,2,3] while `$.*` over the same input
    is undefined.

    Deep-flattening goes through the reference's fn.append, whose Array
    concat yields a plain array rather than a sequence, so once any array
    value has been seen the result escapes sequence normalization: an
    empty or singleton result stays an array (`*` over {"x":[]} is [] and
    over {"x":[1]} is [1], but over {"x":1} it is 1).
    """
    if isinstance(node, dict):
        values: Any = node.values()
    elif isinstance(node, list):
        values = node
    else:
        # Scalars, null, MISSING and functions have no keys.
        return MISSING
    result: list[Any] = []
    plain = False
    for v in values:
        if isinstance(v, list):
            plain = True
            _flatten_into(v, result)
        elif v is not MISSING:
            result.append(v)
    return result if plain else _unwrap_list(result)


def descendant(node: Any) -> Any:
    """The `**` step: every non-array node reachable from the context,
    the context itself included, in document order."""
    if node is MISSING:
        return MISSING
    result: list[Any] = []
    _collect_descendants(node, result)
    return _unwrap_list(result)


def _collect_descendants(node: Any, acc: list[Any]) -> None:
    """The reference's recurseDescendants: an array contributes only its
    members, while EVERY other node -- scalar leaves, null and empty
    objects included -- is emitted before its own children.

    Applying this to a whole sequence is exact: the reference recurses
    into array members without emitting the array, so per-member
    application and whole-sequence application agree.
    """
    if isinstance(node, list):
        for elem in node:
            _collect_descendants(elem, acc)
    elif node is not MISSING:
        acc.append(node)
        if isinstance(node, dict):
            for v in node.values():
                _collect_descendants(v, acc)


def force_array(node: Any) -> Any:
    """Forces node to be a list, wrapping it in a single-element list if
    not already one. Implements the expr[] operator.

    A JSON `null` is a value, not an absence: the reference pushes it into
    a sequence like any other and `[null][0]` is `null`. Only MISSING is
    absent here -- navigation is the place that treats the two alike
    (`null.x` really is undefined), and that lives in field().
    """
    if node is MISSING:
        return MISSING
    if isinstance(node, list):
        return node
    return [node]


class _Rescan:
    """Marks a fused-scan slot whose fast arm did not apply.

    A fused sequence scan (see translator/scan_fusion.py) handles only the
    arm every real document takes -- a field holding a plain int or float.
    The moment it sees anything else it stops trying to be clever and hands
    the slot back as this sentinel; the use site then falls back to the
    original per-operation helper, at its original position, so the unusual
    case is handled by the code that already handles it.
    """

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return "RESCAN"


RESCAN = _Rescan()


def force_array_cons(node: Any) -> Any:
    """`expr[]` over a path whose value may be a constructor's own array.

    An empty array here is the head short-circuit's `cons` result -- a
    single value, not a sequence -- so `[]` promotes it rather than passing
    it through: `a.([].x)[]` is `[[]]`. Any other value is an ordinary
    sequence and force_array applies unchanged, so `nums.([].x)[]` stays
    `[[],[],[]]`.

    Testing the value is sound for the same reason it is in
    map_consarray_step: every empty result that is *not* the short-circuit
    has already collapsed to MISSING before it gets here.
    """
    if isinstance(node, list) and not node:
        return [node]
    return force_array(node)


def filter_(seq: Any, predicate: Callable[[Any], Any]) -> Any:
    """Filters seq by predicate, preserving elements for which the
    predicate returns a truthy value."""
    predicate = deadline_guard(predicate)
    if seq is MISSING:
        return MISSING
    if not isinstance(seq, list):
        return seq if is_truthy(predicate(seq)) else MISSING
    result: list[Any] | None = None
    single: Any = None
    have_single = False
    for elem in seq:
        # A generated predicate returns `bool` essentially always, so
        # answer that inline; is_truthy is only called for results that
        # genuinely need JSONata truthiness coercion.
        r = predicate(elem)
        if r is False or (r is not True and not is_truthy(r)):
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


def filter_field_eq(seq: Any, field_name: str, expected: Any) -> Any:
    """`$seq[field = <literal>]` used as a value.

    The value-producing twin of fn_count_field_eq, monomorphized on the
    expected value's kind for the same reason: the generic shape reaches the
    comparison through a callback, a `field()` call and `eq()` per element,
    and none of that indirection is free in CPython.

    Also the fallback the fused sequence scan (translator/scan_fusion.py)
    redirects to when its fast arm does not apply, which is why it must be a
    plain call taking the field name and literal rather than a callback --
    the scan has no compiled predicate to hand back.

    The lazy single/array pair is load-bearing, not an optimisation: JSONata
    returns nothing for no matches, the element itself for exactly one, and
    an array only for several, and most filters select one element.
    """
    if seq is None or seq is MISSING:
        return MISSING
    if not isinstance(seq, list):
        return seq if _field_eq(seq, field_name, expected) else MISSING
    result: list[Any] | None = None
    single: Any = None
    have_single = False
    for elem in seq:
        if not _field_eq(elem, field_name, expected):
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


def _field_eq(elem: Any, field_name: str, expected: Any) -> bool:
    """`field(elem, name) = expected` for a literal expected value.

    A non-dict element goes the long way round: `field()` maps over a
    sequence and navigates nulls, and the answer for those shapes is
    whatever `eq` makes of what it returns.
    """
    if elem.__class__ is dict or isinstance(elem, dict):
        v = elem.get(field_name, MISSING)
    else:
        v = field(elem, field_name)
    if v is MISSING:
        return False
    ecls = expected.__class__
    if ecls is str:
        return (v.__class__ is str or isinstance(v, str)) and v == expected
    if ecls is bool:
        return v is expected
    if ecls is int or ecls is float:
        vcls = v.__class__
        if vcls is int or vcls is float:
            return float(v) == float(expected)
        return is_number(v) and float(v) == float(expected)
    return bool(_deep_equals(v, expected))


def dynamic_filter(seq: Any, predicate: Callable[[Any], Any]) -> Any:
    """`[expr]` where the predicate is not statically boolean.

    The reference decides index-versus-boolean **per element, on that
    element's own result** -- a number (or an array in which *every* value
    is a number) selects by 0-based index, anything else is a truthiness
    test. Probing the predicate once with an absent context instead, which
    is what this did, gets a predicate that reads the element wrong:
    `objs[x]` over `[{"x":1},{"x":2},{"x":3}]` is *undefined*, because each
    element's own `x` is an index that never equals its own position, and
    the probe saw only MISSING and fell back to a boolean filter.

    A predicate the translator can prove boolean never reaches here -- it
    compiles to `filter_` -- so this is not on the hot path.
    """
    if seq is MISSING:
        return MISSING
    predicate = deadline_guard(predicate)
    items = seq if isinstance(seq, list) else [seq]
    n = len(items)
    result = [item for i, item in enumerate(items) if matches_index_or_truthy(predicate(item), i, n)]
    return _unwrap_list(result)


def matches_index_or_truthy(res: Any, index: int, length: int) -> bool:
    """One predicate result, as the reference's `evaluateFilter` reads it.

    A number selects by 0-based index, negatives counting from the end. An
    array does too, but only when *every* element is a number -- the
    reference's `isArrayOfNumbers` gate is all-or-nothing, so one stray
    non-number disqualifies every number in it.
    """
    if res is True:
        return True
    if res is False or res is MISSING:
        return False
    if isinstance(res, (int, float)) and not isinstance(res, bool):
        idx = int(res)
        return (length + idx if idx < 0 else idx) == index
    if not isinstance(res, list):
        return is_truthy(res)
    if res and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in res):
        for v in res:
            idx = int(v)
            if (length + idx if idx < 0 else idx) == index:
                return True
        return False
    return is_truthy(res)


def subscript(seq: Any, index: Any) -> Any:
    """Returns the element at index (zero-based, negatives count from
    end).

    A JSON `null` is a value, not an absence: the reference pushes it into
    a sequence like any other and `[null][0]` is `null`. Only MISSING is
    absent here -- navigation is the place that treats the two alike
    (`null.x` really is undefined), and that lives in field().
    """
    if seq is MISSING:
        return MISSING
    i = int(to_number(index))
    if not isinstance(seq, list):
        return seq if i in (0, -1) else MISSING
    size = len(seq)
    actual = size + i if i < 0 else i
    return seq[actual] if 0 <= actual < size else MISSING


def range_subscript(seq: Any, from_: Any, to: Any) -> Any:
    """Returns a sub-array containing elements at indices from through to
    (inclusive, zero-based, negatives count from end).

    A JSON `null` is a value, not an absence: the reference pushes it into
    a sequence like any other and `[null][0]` is `null`. Only MISSING is
    absent here -- navigation is the place that treats the two alike
    (`null.x` really is undefined), and that lives in field().
    """
    if seq is MISSING:
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
    """Applies fn with the element as the new context.

    A JSON `null` is a context like any other -- `n.$string($)` is
    `"null"` -- so only MISSING short-circuits.
    """
    if node is MISSING:
        return MISSING
    return fn(node)


def map_step(node: Any, fn: Callable[[Any], Any]) -> Any:
    """Maps fn over every element of a sequence, collecting non-missing
    results. Used for subscript steps inside path expressions.

    A JSON `null` is a context like any other, so only MISSING
    short-circuits.
    """
    fn = deadline_guard(fn)
    if node is MISSING:
        return MISSING
    if isinstance(node, list):
        result: list[Any] = []
        for elem in node:
            val = fn(elem)
            if val is not MISSING:
                _append_to_sequence(result, val)
        return _unwrap_list(result)
    return fn(node)


def consarray_head(head: Any, rest: Callable[[Any], Any]) -> Any:
    """Guards a path whose first step is an array constructor.

    The reference evaluates such a head as a *value* instead of iterating
    it, and breaks out of the step loop as soon as a step yields nothing --
    so an empty constructor is the path's whole result, the remaining steps
    never run, and the empty array escapes the sequence collapse because a
    constructor result is not marked as a sequence. `[].x` is therefore
    `[]` while `([]).x`, `empty.x` and `($e := []; $e.x)` are all undefined.

    `rest` is a thunk, not an already-evaluated value: `[].($error("boom"))`
    must succeed, so the remaining steps genuinely must not run.

    A MISSING head counts as empty. The translator only emits this call
    when the head is statically an array constructor -- which always
    produces an array -- so the only way it can arrive absent is a wrapper
    (`unwrap`, after `[]^(k).x`) having already collapsed the empty one.
    """
    if head is MISSING or (isinstance(head, list) and not head):
        return []
    return rest(head)


def staged_consarray_head(head: Any, rest: Callable[[Any], Any]) -> Any:
    """Guards the step after a constructor head that carried a stage.

    §1's short-circuit returns the head as a *value*, and the reference's
    next step iterates it (`for (ii < input.length)`). A stage or a group can
    leave that value a scalar or an object, which has no length, so the loop
    runs zero times and the path is undefined -- `[1][0]` is `1` but
    `[1][0].$` is not.
    """
    if not isinstance(head, list):
        return MISSING
    return rest(head)


def step_final(node: Any, fn: Callable[[Any], Any]) -> Any:
    """A generic expression step in TERMINAL position.

    Identical to map_step except that a *single* raw result passes through
    verbatim instead of being flattened -- the reference's terminal-step
    rule (`lastStep && result.length === 1 && Array.isArray(result[0])`).
    Only an empty array can tell the two apart in practice: flattening one
    contributes nothing and collapses the whole path to undefined, where the
    reference returns the array. `objs.($zip(one, two))` is `[]`, not
    undefined.

    Two or more results flatten and collapse exactly as map_step does.
    A JSON `null` is a context like any other, so only MISSING
    short-circuits.
    """
    fn = deadline_guard(fn)
    if node is MISSING:
        return MISSING
    if not isinstance(node, list):
        return fn(node)
    raw: list[Any] = []
    for elem in node:
        val = fn(elem)
        if val is not MISSING:
            raw.append(val)
    if len(raw) == 1:
        return raw[0]
    result: list[Any] = []
    for val in raw:
        _append_to_sequence(result, val)
    return _unwrap_list(result)


def call_field_step(node: Any, invoke: Callable[[Any], Any]) -> Any:
    """`a.g(...)` -- a path step whose callee is a FIELD of the step
    context, resolved and invoked per element.

    Not map_step: that treats a JSON null the same as an absent value and
    skips it, but here the callee still has to resolve against null (and
    against a scalar), and failing to resolve is an error rather than an
    absent result -- `$$.g()` over a null input is T1006, not undefined.
    Only a MISSING context and an empty sequence yield undefined without
    invoking anything.
    """
    if node is MISSING:
        return MISSING
    if isinstance(node, list):
        result: list[Any] = []
        for elem in node:
            val = invoke(elem)
            if val is not MISSING:
                _append_to_sequence(result, val)
        return _unwrap_list(result)
    return invoke(node)


def field_function(node: Any, name: str, builtin_name: bool) -> Any:
    """Resolves `name` as a field of `node` for a `a.name(...)` step.

    An absent field is reported as T1005 when the name is also a built-in
    -- `$o.count()` on an object with no `count` field means the author
    probably wanted `$count`, and that is the hint the reference gives --
    and as plain T1006 otherwise. A field that exists but is not callable
    falls through to fn_apply, which reports T1006 itself.
    """
    fn = field(node, name)
    if fn is MISSING:
        if builtin_name:
            raise RuntimeEvaluationError(
                "T1005", f"Attempted to invoke a non-function. Did you mean ${name}?"
            )
        raise RuntimeEvaluationError("T1006", "Attempted to invoke a non-function")
    return fn


def map_group_step(node: Any, fn: Callable[[Any], Any]) -> Any:
    """map_constructor_step for an *object* constructor step.

    Identical, except that each item is passed through group_context first:
    `{...}` is a group expression, so an item that is itself an array is
    grouped over its own elements. `nested.{"k":$}` over `[[1,2],[3]]` is
    `[{"k":[1,2]},{"k":3}]` -- the single-element item collapses, which is
    the only thing separating this from map_constructor_step.
    """
    return map_constructor_step(node, lambda item: fn(group_context(item)))


def map_constructor_step(node: Any, fn: Callable[[Any], Any]) -> Any:
    """Maps fn over every element of a sequence and collects results
    without flattening. Used for array/object constructor steps inside
    path expressions. A JSON `null` is a context like any other, so only
    MISSING short-circuits."""
    fn = deadline_guard(fn)
    if node is MISSING:
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


def map_consarray_step(node: Any, fn: Callable[[Any], Any]) -> Any:
    """Maps a *sub-path led by an array constructor* over a sequence.

    Differs from map_step in one case only: an empty array coming back from
    the sub-path is pushed into the parent sequence whole rather than
    flattened away. That mirrors the reference, where the array a
    constructor builds carries a `cons` flag and `evaluateStep` declines to
    flatten it -- so `nums.([].x)` is `[[],[],[]]`, not the flattened, and
    therefore empty, and therefore undefined, result map_step would give.

    Testing the *value* rather than tracking a flag is sound here because
    the head short-circuit is the only way such a sub-path can yield an
    empty array: every other empty result collapses to MISSING before it
    gets back. A sub-path that actually ran its later steps returns an
    ordinary sequence, which flattens as usual -- `nums.([1,2,3].$)` is
    nine elements, not three arrays.

    The singleton collapse still applies (`objs.([].x)` is `[]`, not
    `[[]]`), but only on the sequence branch. A non-sequence context is
    stepped once and its value returned untouched, which is what keeps the
    empty array alive in `a.([].x)`; `unwrap` would collapse it again.
    """
    fn = deadline_guard(fn)
    if node is None or node is MISSING:
        return MISSING
    if not isinstance(node, list):
        return fn(node)
    result: list[Any] = []
    for elem in node:
        val = fn(elem)
        if isinstance(val, list) and not val:
            result.append(val)
        else:
            _append_to_sequence(result, val)
    return _unwrap_list(result)


def constructor_step_final(node: Any, fn: Callable[[Any], Any]) -> Any:
    """An array-constructor step in TERMINAL position.

    Two rules apply here that `unwrap(map_constructor_step(...))` conflates:

    * the constructor result is never flattened into the parent sequence
      (the reference marks it `cons`), so `nums.[1,2]` keeps three arrays;
    * a *single* raw result passes through verbatim -- the reference's
      terminal-step rule, `if (lastStep && result.length === 1 &&
      Array.isArray(result[0]) && !isSequence(result[0])) resultSequence =
      result[0]`. That is what makes `a.[]` an empty array rather than
      undefined, and `a.[1]` the array `[1]` rather than the number 1.

    The verbatim rule is the sequence branch's singleton collapse and the
    non-sequence branch's "return what the step produced"; both fall out of
    not running the result through `unwrap`.
    """
    fn = deadline_guard(fn)
    if node is None or node is MISSING:
        return MISSING
    if not isinstance(node, list):
        val = fn(node)
        return MISSING if val is MISSING else _unwrap_preserve(val)
    raw: list[Any] = []
    for elem in node:
        val = fn(elem)
        if val is not MISSING:
            raw.append(_unwrap_preserve(val))
    return _unwrap_list(raw)


def constructor_step_keep_singleton(node: Any, fn: Callable[[Any], Any]) -> Any:
    """An array-constructor step under a `[]` (keep-singleton) wrapper.

    `evaluatePath`, at the end:

    ```js
    if (expr.keepSingletonArray) {
        if (Array.isArray(resultSequence) && resultSequence.cons && !resultSequence.sequence) {
            resultSequence = environment.base.createSequence(resultSequence);
        }
        ...
    }
    ```

    A `cons` array is *wrapped* rather than passed through, because `[]`
    promotes a singleton to an array and the constructor's array is a single
    value, not a sequence of one. `a.[1][]` is `[[1]]`.

    Which branch produced the value is exactly the cons/sequence
    distinction: a non-sequence context yields the constructor array itself
    (cons, so wrap), while a sequence context yields a sequence of them
    (already a sequence, so leave it). Doing the finalisation and the wrap
    in one helper is what lets the two be told apart at all -- by value they
    are both just lists.
    """
    fn = deadline_guard(fn)
    if node is None or node is MISSING:
        return MISSING
    if not isinstance(node, list):
        val = fn(node)
        return MISSING if val is MISSING else [_unwrap_preserve(val)]
    raw: list[Any] = []
    for elem in node:
        val = fn(elem)
        if val is not MISSING:
            raw.append(_unwrap_preserve(val))
    return raw if raw else MISSING


def map_constructor_step_flat(node: Any, fn: Callable[[Any], Any]) -> Any:
    """Variant of map_constructor_step for the non-preserve (flatten)
    case."""
    if node is MISSING:
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
        # See mod_d: NaN is not representable as a value here.
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
        # The reference gives NaN here rather than an error. This port has no
        # NaN *value* -- num_node folds it to "absent" -- so producing one
        # would turn `$string(1 % 0)` from D3001 into silently nothing, which
        # is further from the reference than the error is. See section 16.4.
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


def fn_signature_error(argno: int) -> Any:
    """T0410 for an argument count the signature cannot accept."""
    raise RuntimeEvaluationError(
        "T0410", f"Argument {argno} of function does not match function signature"
    )


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
    """`&`. Both sides absent still gives the empty string: the reference
    casts each operand with `undefined -> ""` before joining, and has no
    both-absent short-circuit. `$length(nope & nope)` is 0."""
    sa = "" if a is MISSING else to_text(a)
    sb = "" if b is MISSING else to_text(b)
    return sa + sb


# =============================================================================
# Comparisons
# =============================================================================


def eq(a: Any, b: Any) -> bool:
    """`=` -- the scalar cases are settled here rather than in
    _deep_equals, so a string or numeric comparison (the overwhelming
    majority of predicate comparisons) costs one call instead of two.

    MISSING cannot be a str/bool/int/float, so the fast arms can run
    ahead of the MISSING test without observing it.

    ta is str/bool goes through bool(a == b) and `not bool(a == b)`, not
    `a == b`/`a != b`, so the result is exactly what _deep_equals would
    have returned for a subclass whose __eq__ returns a non-bool or whose
    __ne__ is not the negation of its __eq__.
    """
    ta = a.__class__
    tb = b.__class__
    if ta is tb and (ta is str or ta is bool):
        return bool(a == b)
    if (ta is int or ta is float) and (tb is int or tb is float):
        return float(a) == float(b)
    if a is MISSING or b is MISSING:
        return False
    return _deep_equals(a, b)


def ne(a: Any, b: Any) -> bool:
    """`!=` -- the mirror of eq; see its docstring."""
    ta = a.__class__
    tb = b.__class__
    if ta is tb and (ta is str or ta is bool):
        return not bool(a == b)
    if (ta is int or ta is float) and (tb is int or tb is float):
        return float(a) != float(b)
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


def object_of_distinct(keys: list[str], values: list[Any]) -> dict[str, Any]:
    """object_of for a constructor whose literal keys the translator has
    already proved distinct.

    The duplicate check object_of performs is a dict lookup per key, and it
    can only ever fire when two *literal* keys collide -- which is a property
    of the expression text, decidable once at compile time rather than on
    every evaluation. This is the Python-shaped half of the JVM port's
    `objectOfDistinct`: there is no per-key node to avoid allocating here, a
    dict literal being one allocation already, but the check itself is real.
    """
    result: dict[str, Any] = {}
    for k, value in zip(keys, values, strict=True):
        # None here is JSON null (D1), a real value -- only MISSING, an
        # unpopulated slot, is skipped.
        if value is not MISSING:
            result[k] = value
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
    _string_builtins = _strings()

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
    _numeric_builtins = _numeric()

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
    # num_node, not math.floor's raw int: math.floor returns an
    # arbitrary-precision int, so $floor(1e21) became an exact
    # 10**21 rather than the double 1e21 a JSONata number is, and then
    # rendered as "1000000000000000000000" instead of "1e+21".
    return num_node(float(math.floor(to_number(arg))))


def fn_ceil(arg: Any) -> Any:
    if arg is MISSING:
        return MISSING
    return num_node(float(math.ceil(to_number(arg))))


def fn_round(arg: Any, precision: Any = MISSING) -> Any:
    _numeric_builtins = _numeric()

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
    _numeric_builtins = _numeric()

    return _numeric_builtins.fn_random()


def fn_formatBase(number: Any, radix: Any = MISSING) -> Any:
    _numeric_builtins = _numeric()

    return _numeric_builtins.fn_formatBase(number, radix)


def fn_formatNumber(number: Any, picture: Any, options: Any = MISSING) -> Any:
    _numeric_builtins = _numeric()

    return _numeric_builtins.fn_formatNumber(number, picture, options)


def fn_formatInteger(number: Any, picture: Any) -> Any:
    _numeric_builtins = _numeric()

    return _numeric_builtins.fn_formatInteger(number, picture)


def fn_parseInteger(string: Any, picture: Any) -> Any:
    _numeric_builtins = _numeric()

    return _numeric_builtins.fn_parseInteger(string, picture)


# =============================================================================
# Built-in functions -- string (thin delegations to runtime.strings.builtins)
# =============================================================================


def _strings() -> ModuleType:
    global _strings_mod
    mod = _strings_mod
    if mod is None:
        from .strings import builtins as string_builtins

        mod = _strings_mod = string_builtins
    return mod


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
    """Fused $count(seq[field = value]).

    Monomorphized on the expected value's kind. The previous shape built
    a `matches` closure, then reached it per element through a
    `_field_matches` helper called from `sum(1 for ...)` -- three
    Python-level frames per element, plus a `float(expected)` conversion
    per element in the numeric case. CPython resumes a generator frame
    per item (PEP 709 inlines list/dict/set comprehensions, NOT generator
    expressions), so none of that indirection is free the way the JS
    port's equivalent is once V8 inlines it. One flat loop per kind with
    the comparison as a branch measures 3.0x on a 24-element sequence.

    Exact-type identity tests come first with the `isinstance` they
    replace kept as the fallback arm of the same `or`, so `str`/`dict`
    subclasses still take the path they always did.
    """
    if seq is None or seq is MISSING:
        return 0
    items = seq if isinstance(seq, list) else (seq,)
    count = 0
    ecls = expected.__class__
    if ecls is str or isinstance(expected, str):
        for elem in items:
            if elem.__class__ is dict or isinstance(elem, dict):
                v = elem.get(field_name, MISSING)
                if (v.__class__ is str or isinstance(v, str)) and v == expected:
                    count += 1
        return count
    if ecls is bool:
        # `bool` cannot be subclassed, so `isinstance(v, bool) and
        # v == expected` is exactly `v is expected`.
        for elem in items:
            if (elem.__class__ is dict or isinstance(elem, dict)) and elem.get(
                field_name, MISSING
            ) is expected:
                count += 1
        return count
    if ecls is int or ecls is float or is_number(expected):
        # float(expected) was recomputed per element before; it is a
        # loop invariant.
        expected_f = float(expected)
        for elem in items:
            if elem.__class__ is dict or isinstance(elem, dict):
                v = elem.get(field_name, MISSING)
                vcls = v.__class__
                if vcls is int or vcls is float:
                    if float(v) == expected_f:
                        count += 1
                elif is_number(v) and float(v) == expected_f:
                    count += 1
        return count
    for elem in items:
        if elem.__class__ is dict or isinstance(elem, dict):
            v = elem.get(field_name, MISSING)
            if v is not MISSING and _deep_equals(v, expected):
                count += 1
    return count


def fn_count_filter(seq: Any, predicate: Callable[[Any], Any]) -> int:
    if seq is MISSING:
        return 0
    if not isinstance(seq, list):
        return 1 if is_truthy(predicate(seq)) else 0
    # A plain `for` rather than `sum(1 for ...)`: the generator costs a
    # frame resume per element. A generated predicate returns `bool`
    # essentially always, so answer that inline and only call is_truthy
    # for the shapes that actually need coercion.
    count = 0
    for elem in seq:
        r = predicate(elem)
        if r is True or (r is not False and is_truthy(r)):
            count += 1
    return count


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
    """Fused $sum(arr.field) / $sum(arr.f1.f2).

    The single-level walk has an exact-type fast arm for a numeric field
    value, which is what essentially every element of real JSON input
    hits. It skips three things the general arm pays per element: the
    `(v1,)` one-tuple plus iterator setup that a scalar needed in order
    to be walked by a `for`, the `_require_t0412` call (whose entire fast
    path is the exact-type test now done inline), and the `float()`
    conversion (adding an int to a float already converts it, with the
    same rounding). Measured 1.77x on a 2000-element sequence.

    Ordering note: MISSING is tested *after* the numeric arm rather than
    before it. MISSING's class is `_Missing`, so it can never reach the
    numeric arm, and the common case then costs one type test instead of
    a type test plus an identity test.
    """
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
        if field_name2 is not None:
            if v1 is MISSING:
                continue
            for sub in v1 if isinstance(v1, list) else (v1,):
                if not isinstance(sub, dict):
                    continue
                v2 = sub.get(field_name2, MISSING)
                if v2 is MISSING:
                    continue
                for n in v2 if isinstance(v2, list) else (v2,):
                    ncls = n.__class__
                    if ncls is not int and ncls is not float:
                        _require_t0412(n, "$sum")
                    total += float(n)
                    any_ = True
            continue
        vcls = v1.__class__
        if vcls is int or vcls is float:
            total += v1
            any_ = True
        elif v1 is MISSING:
            continue
        elif vcls is list or isinstance(v1, list):
            for n in v1:
                ncls = n.__class__
                if ncls is not int and ncls is not float:
                    _require_t0412(n, "$sum")
                total += float(n)
                any_ = True
        else:
            _require_t0412(v1, "$sum")
            total += float(v1)
            any_ = True
    return num_node(total) if any_ else MISSING


def fn_average_field(seq: Any, field_name: str, field_name2: str | None = None) -> Any:
    """Fused $average(arr.field) / $average(arr.f1.f2). Same exact-type
    fast arm as fn_sum_field -- see its docstring for the reasoning."""
    if seq is MISSING:
        return MISSING
    total = 0.0
    count = 0
    for elem in seq if isinstance(seq, list) else (seq,):
        if not isinstance(elem, dict):
            continue
        v1 = elem.get(field_name, MISSING)
        if field_name2 is not None:
            if v1 is MISSING:
                continue
            for sub in v1 if isinstance(v1, list) else (v1,):
                if not isinstance(sub, dict):
                    continue
                v2 = sub.get(field_name2, MISSING)
                if v2 is MISSING:
                    continue
                for n in v2 if isinstance(v2, list) else (v2,):
                    ncls = n.__class__
                    if ncls is not int and ncls is not float:
                        _require_average_arg(n)
                    total += float(n)
                    count += 1
            continue
        vcls = v1.__class__
        if vcls is int or vcls is float:
            total += v1
            count += 1
        elif v1 is MISSING:
            continue
        elif vcls is list or isinstance(v1, list):
            for n in v1:
                ncls = n.__class__
                if ncls is not int and ncls is not float:
                    _require_average_arg(n)
                total += float(n)
                count += 1
        else:
            _require_average_arg(v1)
            total += float(v1)
            count += 1
    return MISSING if count == 0 else num_node(total / count)


def fn_max_field(seq: Any, field_name: str, field_name2: str | None = None) -> Any:
    """Fused $max(arr.field) / $max(arr.f1.f2). Same exact-type fast arm
    as fn_sum_field, except that the float() conversion is KEPT: `best`
    feeds num_node, which returns a big int unrounded but rounds the
    float form of the same value, so dropping float() here would change
    the result for integers beyond int64."""
    if seq is MISSING:
        return MISSING
    best = -math.inf
    any_ = False
    for elem in seq if isinstance(seq, list) else (seq,):
        if not isinstance(elem, dict):
            continue
        v1 = elem.get(field_name, MISSING)
        if field_name2 is not None:
            if v1 is MISSING:
                continue
            for sub in v1 if isinstance(v1, list) else (v1,):
                if not isinstance(sub, dict):
                    continue
                v2 = sub.get(field_name2, MISSING)
                if v2 is MISSING:
                    continue
                for n in v2 if isinstance(v2, list) else (v2,):
                    ncls = n.__class__
                    if ncls is not int and ncls is not float:
                        _require_t0412(n, "$max")
                    d = float(n)
                    if d > best:
                        best = d
                    any_ = True
            continue
        vcls = v1.__class__
        if vcls is int or vcls is float:
            d = float(v1)
            if d > best:
                best = d
            any_ = True
        elif v1 is MISSING:
            continue
        elif vcls is list or isinstance(v1, list):
            for n in v1:
                ncls = n.__class__
                if ncls is not int and ncls is not float:
                    _require_t0412(n, "$max")
                d = float(n)
                if d > best:
                    best = d
                any_ = True
        else:
            _require_t0412(v1, "$max")
            d = float(v1)
            if d > best:
                best = d
            any_ = True
    return num_node(best) if any_ else MISSING


def fn_min_field(seq: Any, field_name: str, field_name2: str | None = None) -> Any:
    """Fused $min(arr.field) / $min(arr.f1.f2). See fn_max_field."""
    if seq is MISSING:
        return MISSING
    best = math.inf
    any_ = False
    for elem in seq if isinstance(seq, list) else (seq,):
        if not isinstance(elem, dict):
            continue
        v1 = elem.get(field_name, MISSING)
        if field_name2 is not None:
            if v1 is MISSING:
                continue
            for sub in v1 if isinstance(v1, list) else (v1,):
                if not isinstance(sub, dict):
                    continue
                v2 = sub.get(field_name2, MISSING)
                if v2 is MISSING:
                    continue
                for n in v2 if isinstance(v2, list) else (v2,):
                    ncls = n.__class__
                    if ncls is not int and ncls is not float:
                        _require_t0412(n, "$min")
                    d = float(n)
                    if d < best:
                        best = d
                    any_ = True
            continue
        vcls = v1.__class__
        if vcls is int or vcls is float:
            d = float(v1)
            if d < best:
                best = d
            any_ = True
        elif v1 is MISSING:
            continue
        elif vcls is list or isinstance(v1, list):
            for n in v1:
                ncls = n.__class__
                if ncls is not int and ncls is not float:
                    _require_t0412(n, "$min")
                d = float(n)
                if d < best:
                    best = d
                any_ = True
        else:
            _require_t0412(v1, "$min")
            d = float(v1)
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
        # $reverse's signature is <a:a>, so a non-array argument is
        # array-wrapped by the coercion rules rather than passed through:
        # $reverse("ab") is ["ab"], not "ab".
        return [arg]
    return list(reversed(arg))


def _structural_signature(v: Any) -> int:
    """A hash consistent with _deep_equals: values that compare equal
    always produce the same signature (unequal values usually do not).

    Object keys are combined additively so the result is independent of
    key order, because _deep_equals ignores it. Numbers hash through
    float() so 1 and 1.0 -- which _deep_equals equates -- agree. A
    collision only costs one _deep_equals call, which then correctly
    reports "not equal", so over-collision is safe and under-collision
    (splitting equal values apart) is the only real hazard.
    """
    cls = v.__class__
    if cls is str:
        return hash(v)
    if cls is bool:
        return 1 if v else 2
    if cls is int or cls is float:
        try:
            return hash(float(v))
        except OverflowError:
            return 0
    if v is None:
        return 3
    if isinstance(v, list):
        h = 0x01000193 ^ len(v)
        for e in v:
            h = (h * 31 + _structural_signature(e)) & 0xFFFFFFFFFFFFFFFF
        return h
    if isinstance(v, dict):
        h = 0x811C9DC5 ^ len(v)
        for k, val in v.items():
            h = (h + ((hash(k) ^ _structural_signature(val)) * 0x85EBCA6B)) & 0xFFFFFFFFFFFFFFFF
        return h
    # A str/int/float SUBCLASS nested inside a composite must land on the
    # same signature as its exact-type equal, because _deep_equals
    # compares by *kind*: {"a": MyStr("x")} and {"a": "x"} are equal, so
    # they have to share a bucket or the pair is never even compared.
    if isinstance(v, str):
        return hash(v)
    if is_number(v):
        try:
            return hash(float(v))
        except OverflowError:
            return 0
    return 0


def _first_deep_equal(candidates: list[Any], elem: Any) -> bool:
    """Whether elem is _deep_equals to anything in candidates. Cold: only
    reached on a _structural_signature collision or from _distinct_scan."""
    return any(_deep_equals(elem, seen) for seen in candidates)


def _distinct_scan(arg: list[Any]) -> list[Any]:
    """$distinct's original pairwise algorithm, kept verbatim as the
    fallback for inputs the kind-partitioned fast path cannot speak
    for."""
    result: list[Any] = []
    for elem in arg:
        if not _first_deep_equal(result, elem):
            result.append(elem)
    return result


def fn_distinct(arg: Any) -> Any:
    """$distinct(array) -- first-occurrence order preserved.

    Was an O(n^2) _deep_equals scan against every kept element (42 ms
    for a 2000-element string sequence with 500 distinct values).
    Scalars now deduplicate through a set per JSONata *kind* -- a set per
    kind, not one shared set, because Python's own equality conflates
    pairs that _deep_equals keeps distinct (True == 1, False == 0) and a
    single set would silently drop one of them. Composites still need
    _deep_equals, but only against candidates sharing a
    _structural_signature, so the comparison count is near-linear.

    Anything whose exact class is not one of the seven JSON classes hands
    the *whole* list to _distinct_scan: a str/int/float subclass is
    _deep_equals-equal to its exact-type kin but would be appended
    without registering in that kind's set, so a later exact-type
    duplicate would survive. Restarting costs one wasted pass on input
    that never occurs in JSON-derived data, and makes the result
    identical to the original algorithm by construction.

    NaN needs no special case: _deep_equals(nan, nan) is False (float
    inequality), and CPython >= 3.10 hashes each NaN object by identity,
    so distinct NaN objects land in distinct set slots and stay
    undeduplicated exactly as before.
    """
    if arg is MISSING:
        return MISSING
    if not isinstance(arg, list):
        return arg
    result: list[Any] = []
    append = result.append
    seen_str: set[str] | None = None
    seen_num: set[float] | None = None
    seen_bool = 0  # bit 1 = False already kept, bit 2 = True already kept
    seen_null = False
    buckets: dict[int, list[Any]] | None = None
    for elem in arg:
        cls = elem.__class__
        if cls is str:
            if seen_str is None:
                seen_str = {elem}
            elif elem in seen_str:
                continue
            else:
                seen_str.add(elem)
        elif cls is int or cls is float:
            try:
                key = float(elem)
            except OverflowError:
                # An int too large for a float: _deep_equals raises on it
                # too, so let the scan path raise it identically.
                return _distinct_scan(arg)
            if key != key:  # NaN -- never equal to itself, never deduplicated
                append(elem)
                continue
            if seen_num is None:
                seen_num = {key}
            elif key in seen_num:
                continue
            else:
                seen_num.add(key)
        elif cls is bool:
            bit = 2 if elem else 1
            if seen_bool & bit:
                continue
            seen_bool |= bit
        elif cls is dict or cls is list:
            signature = _structural_signature(elem)
            if buckets is None:
                buckets = {signature: [elem]}
            else:
                bucket = buckets.get(signature)
                if bucket is None:
                    buckets[signature] = [elem]
                elif _first_deep_equal(bucket, elem):
                    continue
                else:
                    bucket.append(elem)
        elif elem is None:
            if seen_null:
                continue
            seen_null = True
        else:
            return _distinct_scan(arg)
        append(elem)
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
    _fn_shuffle = _sequences().fn_shuffle

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
    set_timeout(). Only installed when a deadline is actually active.

    Fourteen runtime helpers call this on entry, so the *untimed* answer
    has to be nearly free: `_DEADLINE_STACK` is context.py's list
    object, and an empty one settles it in a global load plus a
    truthiness test rather than the ~160 ns
    has_deadline() -> _current() -> ContextVar.get() chain it used to
    take on every single call.
    """
    if not _DEADLINE_STACK:
        return fn
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
    fn_apply = _lambdas().fn_apply

    return lambda elem: fn_apply(fn, elem)


def tuple_callback(fn: Any) -> Callable[[Any], Any]:
    """Adapts a function value to a callback that receives a tuple when
    the function declares two or more parameters, its first slot
    otherwise."""
    _lam = _lambdas()
    fn_apply, lambda_arity = _lam.fn_apply, _lam.lambda_arity

    if lambda_arity(fn) >= 2:
        return lambda t: fn_apply(fn, t)
    return lambda t: fn_apply(fn, t[0])


def fn_map(arr: Any, fn: Any) -> Any:
    _seq = _sequences()
    require_function(fn, 2)
    lambda_arity = _lambdas().lambda_arity

    if not isinstance(fn, JLambda):
        # A literal/generated callback (plain Python callable) -- the
        # translator already picked the right call site by arity.
        return _seq.fn_map(arr, fn)
    if lambda_arity(fn) >= 2:
        return _seq.fn_map_indexed(arr, tuple_callback(fn))
    return _seq.fn_map(arr, element_callback(fn))


def fn_filter(arr: Any, predicate: Any) -> Any:
    _seq = _sequences()
    require_function(predicate, 2)
    lambda_arity = _lambdas().lambda_arity

    if not isinstance(predicate, JLambda):
        return _seq.fn_filter(arr, predicate)
    if lambda_arity(predicate) >= 2:
        return _seq.fn_filter_indexed(arr, tuple_callback(predicate))
    return _seq.fn_filter(arr, element_callback(predicate))


def require_function(value: Any, argno: int, optional: bool = False) -> Any:
    """T0410 for a non-function where a built-in's signature demands one.

    The higher-order built-ins compile their callback at the call site
    rather than receiving it as an ordinary argument, so the translator's
    inline signature check never sees that position -- `$sort(nums, 1)`
    reaches the runtime with a number where a comparator belongs. Checking
    it here is the one place the value actually arrives.
    """
    if value is MISSING:
        # A required function parameter is not satisfied by an absent
        # argument -- its signature fragment is `f`, which no `m` matches.
        if optional:
            return value
        raise RuntimeEvaluationError(
            "T0410", f"Argument {argno} of function does not match function signature"
        )
    if not is_function(value) and not callable(value):
        raise RuntimeEvaluationError(
            "T0410", f"Argument {argno} of function does not match function signature"
        )
    return value


def fn_single(arr: Any, predicate: Any = MISSING) -> Any:
    _seq = _sequences()
    require_function(predicate, 2, optional=True)
    lambda_arity = _lambdas().lambda_arity

    if predicate is MISSING:
        return _seq.fn_single_one_arg(arr)
    if not isinstance(predicate, JLambda):
        return _seq.fn_single(arr, predicate)
    if lambda_arity(predicate) >= 2:
        return _seq.fn_single_indexed(arr, tuple_callback(predicate))
    return _seq.fn_single(arr, element_callback(predicate))


def fn_sift(obj: Any, fn: Any) -> Any:
    _seq = _sequences()
    require_function(fn, 2, optional=True)

    if not isinstance(fn, JLambda):
        return _seq.fn_sift(obj, fn)
    return _seq.fn_sift(obj, tuple_callback(fn))


def fn_each(obj: Any, fn: Any) -> Any:
    _seq = _sequences()
    require_function(fn, 2)

    if not isinstance(fn, JLambda):
        return _seq.fn_each(obj, fn)
    return _seq.fn_each(obj, tuple_callback(fn))


def fn_sort(arr: Any, fn: Any = MISSING) -> Any:
    _seq = _sequences()
    require_function(fn, 2, optional=True)
    lambda_arity = _lambdas().lambda_arity

    if fn is MISSING:
        return _seq.fn_sort(arr, None)
    if not isinstance(fn, JLambda):
        return _seq.fn_sort(arr, fn)
    if lambda_arity(fn) >= 2:
        return _seq.fn_sort_comparator(arr, tuple_callback(fn))
    return _seq.fn_sort(arr, element_callback(fn))


def fn_sort_comparator(arr: Any, comparator_fn: Callable[[Any], Any]) -> Any:
    _seq = _sequences()

    return _seq.fn_sort_comparator(arr, comparator_fn)


def fn_sort_by_ordering_key(arr: Any, key_fn: Callable[[Any], Any], descending: bool) -> Any:
    _seq = _sequences()

    return _seq.fn_sort_by_ordering_key(arr, key_fn, descending)


def fn_reduce(arr: Any, fn: Callable[[Any], Any], init: Any = MISSING) -> Any:
    require_function(fn, 2)
    return _sequences().fn_reduce(arr, fn, init)


def fn_reduce_dynamic(arr: Any, fn: Any, init: Any = MISSING) -> Any:
    """$reduce where the reducer is an *expression* rather than a literal
    lambda, so its arity is only known now.

    The translator checks a literal `function($a){...}` at compile time,
    but a variable holding one -- or a built-in like `$sum` -- reached
    fn_reduce unchecked and was invoked with a 4-element tuple, yielding
    nonsense ([1,2,1,[1,2]]) instead of D3050.
    """
    # A non-function is a *signature* mismatch (`<afj?:j>` demands `f` at
    # position 2), which the reference reports as T0410. D3050 is reserved
    # for a real function whose arity is wrong.
    require_function(fn, 2)
    if lambda_arity(fn) < 2:
        raise RuntimeEvaluationError(
            "D3050", "The second argument of $reduce must accept at least 2 parameters"
        )
    from .lambdas import fn_apply

    return _sequences().fn_reduce(arr, lambda elem: fn_apply(fn, elem), init)


def fn_map_indexed(arr: Any, fn: Callable[[Any], Any]) -> Any:
    _seq = _sequences()

    return _seq.fn_map_indexed(arr, fn)


def fn_filter_indexed(arr: Any, predicate: Callable[[Any], Any]) -> Any:
    _seq = _sequences()

    return _seq.fn_filter_indexed(arr, predicate)


def fn_single_indexed(arr: Any, predicate: Callable[[Any], Any]) -> Any:
    _seq = _sequences()

    return _seq.fn_single_indexed(arr, predicate)


def fn_collect_pairs(source: Any, elem_fn: Callable[[Any], Any]) -> Any:
    _seq = _sequences()

    return _seq.fn_collect_pairs(source, elem_fn)


def fn_collect_triples(grandparents: Any, parent_fn: Callable[[Any], Any], elem_fn: Callable[[Any], Any]) -> Any:
    _seq = _sequences()

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
        _collect_keys(obj, seen)
        if not seen:
            return MISSING
        return _unwrap_list(list(seen.keys()))
    if not isinstance(obj, dict):
        return MISSING
    keys = list(obj.keys())
    return MISSING if not keys else _unwrap_list(keys)


def _collect_keys(seq: list[Any], seen: dict[str, None]) -> None:
    """Union of keys, first-seen order, recursing into nested arrays --
    the reference flattens the whole structure, so
    $keys([{"a":1},[{"b":2}]]) is ["a", "b"] rather than just ["a"]."""
    for elem in seq:
        if isinstance(elem, dict):
            for k in elem:
                seen[k] = None
        elif isinstance(elem, list):
            _collect_keys(elem, seen)


def fn_clone(arg: Any) -> Any:
    """$clone(object) -- a deep copy, as used by the transform operator.
    Signature <(oa):o>, so a string/number/function argument is T0410."""
    if arg is MISSING:
        return MISSING
    if not isinstance(arg, (dict, list)):
        raise RuntimeEvaluationError("T0410", "$clone: argument must be an object or array")
    return _deep_copy(arg)


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
            # Recurse into a nested array rather than skipping it: the
            # reference flattens the whole structure, so
            # $lookup([{"a":1},[{"a":2}]], "a") is [1, 2].
            v = fn_lookup(elem, key) if isinstance(elem, (dict, list)) else MISSING
            if v is not MISSING:
                _append_to_sequence(result, v)
        return _unwrap_list(result)
    return MISSING


def fn_spread(obj: Any) -> Any:
    if obj is MISSING:
        return MISSING
    if isinstance(obj, list):
        # Array input: spread each element, but preserve the array result
        # (the reference's fn.append returns a cons-sequence, which is not
        # singleton-collapsed by the evaluator).
        result: list[Any] = []
        for elem in obj:
            s = fn_spread(elem)
            if s is not MISSING:
                _append_to_sequence(result, s)
        return result or MISSING
    if not isinstance(obj, dict):
        return obj
    # Object: spread to one single-key dict per key, then singleton-collapse
    # (mirrors the reference's non-cons sequence → evaluator singleton rule).
    return _unwrap_list([{k: v} for k, v in obj.items()])


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
    _iso = _iso_module()

    if picture is MISSING:
        return _iso.millis_to_iso(_ctx.evaluation_millis())
    tz = None if timezone is MISSING else to_text(timezone)
    return _iso.millis_to_picture(_ctx.evaluation_millis(), to_text(picture), tz)


def fn_millis() -> Any:
    return _ctx.evaluation_millis()


def fn_fromMillis(millis: Any, picture: Any = MISSING, timezone: Any = MISSING) -> Any:
    _iso = _iso_module()

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
    _iso = _iso_module()

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
    _fn_pipe = _lambdas().fn_pipe

    return _fn_pipe(arg, fn)


def fn_apply(fn: Any, arg: Any) -> Any:
    return _lambdas().fn_apply(fn, arg)


def fn_apply_tco(fn: Any, arg: Any) -> Any:
    _fn_apply_tco = _lambdas().fn_apply_tco

    return _fn_apply_tco(fn, arg)


def lambda_value(fn: Callable[[Any], Any], arity: int = -1) -> JLambda:
    return JLambda(fn, arity)


def lambda_arity(fn: Any) -> int:
    return cast(int, _lambdas().lambda_arity(fn))


def regex_value(pattern: str, flags: str) -> Any:
    return _regex_ops_module().regex_value(pattern, flags)


# =============================================================================
# Bindings support (thin delegations to context.py)
# =============================================================================


def resolve_binding(name: str) -> Any:
    v = _ctx.resolve_binding(name)
    return v if v is not MISSING else builtin_value(name)


@lru_cache(maxsize=256)
def builtin_value(name: str) -> Any:
    """A built-in as a first-class value: `$map(x, $abs)`, `x ~> $abs`.

    Every built-in the reference registers is a function value there, so a
    hand-maintained table of the ones this port happened to need left the
    rest resolving to nothing -- `$abs` was MISSING, and passing it anywhere
    silently did nothing. Built from the same signature table the call sites
    use, so a built-in cannot be in one and not the other.
    """
    from ..translator.translator import _BUILTIN_SIGNATURES

    sig = _BUILTIN_SIGNATURES.get(name)
    fn = globals().get("fn_" + name)
    if sig is None or fn is None or not callable(fn):
        return MISSING
    from .signature import arity_bounds

    bounds = arity_bounds(sig)
    arity = 1 if bounds is None or bounds[1] <= 0 else bounds[1]
    if arity == 1:
        return JLambda(lambda b: fn(b), 1)

    def call(b: Any, _fn: Any = fn, _n: int = arity) -> Any:
        args = list(b) if isinstance(b, list) else [b]
        args.extend([MISSING] * (_n - len(args)))
        return _fn(*args[:_n])

    return JLambda(call, arity)


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

    Exact-type first: a small int renders as str(n) directly, where the
    general path would build a float and run the notation rules. bool is
    an int *subclass*, so True.__class__ is bool and it correctly falls
    through to _to_text_general, which still renders "true"/"false".

    The str(n) shortcut is bounded at 2**53. A JSONata number is an IEEE
    double, so 12345678901234567890 IS the double 12345678901234567168
    and must render as "12345678901234567000" the way the reference
    does; str(n) would print the exact integer instead. Below 2**53 the
    int is exactly representable and smaller than 1e21, so str(n) and
    the double rendering agree character for character.
    """
    t = n.__class__
    if t is str:
        return cast(str, n)  # `type(n) is str` is exact; mypy cannot narrow on it
    if t is int:
        if -_EXACT_INT_MAX <= n <= _EXACT_INT_MAX:
            return str(n)
        return _int_to_text(n)
    if t is float:
        return number_to_string(n)
    return _to_text_general(n)


def _int_to_text(n: int) -> str:
    """An int too large to be exactly a double. float() it and use the
    double rules; an int beyond the double range becomes Infinity, which
    is what the equivalent JavaScript Number would already be."""
    try:
        return number_to_string(float(n))
    except OverflowError:
        return "-Infinity" if n < 0 else "Infinity"


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
    _json = _json_module

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
    _json = _json_module

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


# 2**53: beyond this an int is no longer exactly representable as a double,
# so str(n) stops agreeing with the double-based rendering JSONata specifies.
_EXACT_INT_MAX = 9007199254740992


def _js_number_to_string(v: float) -> str:
    """ECMA-262 `Number::toString(v, 10)`, which is what JSONata numbers
    render as (jsonata2js `string.js#fn_string` reaches it through
    JavaScript's own `String(number)`).

    repr() of a Python float is already the shortest round-tripping
    decimal form, which is exactly the (s, k, n) triple the spec's step 5
    selects -- so the digits come from repr() and only the *placement*
    rules below are ported. Validated against Node's `String(x)` over
    1232 doubles including subnormals, both 1e21/1e-7 notation
    boundaries, and 1200 randomised magnitudes spanning the whole
    exponent range: zero mismatches.
    """
    if v != v:
        return "NaN"
    if v == 0:
        return "0"  # both zeros: JS String(-0) is "0"
    if v == math.inf:
        return "Infinity"
    if v == -math.inf:
        return "-Infinity"
    sign = "-" if v < 0 else ""
    r = repr(-v if v < 0 else v)
    if "e" in r:
        mant, _, exp = r.partition("e")
        e10 = int(exp)
    else:
        mant, e10 = r, 0
    ip, _, fp = mant.partition(".")
    raw = ip + fp
    stripped = raw.lstrip("0")
    digits = stripped.rstrip("0") or "0"
    k = len(digits)
    # value == 0.<digits> * 10**n
    n = len(ip) + e10 - (len(raw) - len(stripped))
    if k <= n <= 21:
        return sign + digits + "0" * (n - k)
    if 0 < n <= 21:
        return sign + digits[:n] + "." + digits[n:]
    if -6 < n <= 0:
        return sign + "0." + "0" * -n + digits
    e = n - 1
    head = digits if k == 1 else digits[0] + "." + digits[1:]
    return f"{sign}{head}e{'+' if e >= 0 else '-'}{abs(e)}"


def number_to_string(v: float) -> str:
    """JSONata's number rendering: a non-integral value is first rounded
    to 15 significant digits (float noise normalisation), then rendered
    by ECMA-262 Number::toString. Mirrors jsonata2js `string.js:93`
    (`String(Number.isInteger(arg) ? arg : Number(arg.toPrecision(15)))`).

    Hot: every $string / `&` of a number reaches here, so the two shapes
    that dominate answer without touching _js_number_to_string's digit
    surgery at all.

    * Integral and inside the notation window: `str(int(v))` IS the
      spec's output, since ECMA only leaves plain notation at 1e21.
    * Otherwise `%.15g` is exactly the round-to-15-significant-digits
      step, done in C. When it comes back WITHOUT an exponent it has
      also already produced the spec's plain form -- %g leaves plain
      notation below 1e-4, which is inside ECMA's 1e-6 boundary, and
      above that boundary both agree -- and %g strips trailing zeros
      just as the spec does. Anything with an exponent (either
      notation's boundary, subnormals, huge magnitudes) goes the long
      way, re-quantised through float() first so the 15-digit rounding
      is actually applied.
    """
    if v != v or v == math.inf or v == -math.inf:
        return _js_number_to_string(v)
    if v.is_integer():
        # Bounded at 2**53, not at the 1e21 notation boundary: above
        # 2**53 a double's EXACT integer value has more digits than its
        # shortest round-tripping form, and the spec renders the latter
        # (12345678901234567168 must print as "12345678901234567000").
        if -_EXACT_INT_MAX <= v <= _EXACT_INT_MAX:
            return str(int(v))
        return _js_number_to_string(v)
    s = f"{v:.15g}"
    if "e" not in s:
        return s
    return _js_number_to_string(float(s))


def _floor_f(v: float) -> float:
    """math.floor returns an int, which for |v| beyond 2**53 costs an
    exact-integer conversion just to answer "is this integral"."""
    return float(math.floor(v))


def clamp_index(i: int, length: int) -> int:
    if i < 0:
        i = max(0, length + i)
    return min(i, length)


def group_context(node: Any) -> Any:
    """The context an object constructor's value expressions actually see.

    `{...}` is a *group expression* in the reference, and
    evaluateGroupExpression iterates its input: an array context is grouped
    over its **elements**, not treated as one value. With literal keys every
    element lands in one group, whose data the reference accumulates with
    fn.append -- so the value context is the append-fold of the elements,
    which collapses a single-element array (`[3]` becomes `3`) and flattens
    one level of nesting. For anything that is not an array this is the
    identity, which is the overwhelmingly common case.
    """
    if not isinstance(node, list):
        return node
    if not node:
        return MISSING
    data = node[0]
    for item in node[1:]:
        data = fn_append(data, item)
    return data


def context_step(node: Any, is_last: bool) -> Any:
    """The `.$` path step: an identity map that is still a *step*.

    `$` re-binds the context to itself, so the per-element result is the
    element -- but the reference then finalises the step's results like any
    other step's, and that finalisation spreads an element that is a plain
    array. `nested.$` over `[[1,2],[3]]` is therefore `[1,2,3]`, not the two
    inner arrays, which is the only thing separating it from an identity.

    Two arms, both the reference's: a single raw array as the *last* step's
    only result passes through verbatim (`one.$` over `[[9]]` is `[9]`),
    and a constructor's own array is `cons` and is never spread.
    """
    if node is MISSING:
        return MISSING
    if not isinstance(node, list):
        return node
    if is_last and len(node) == 1 and isinstance(node[0], list) and not isinstance(node[0], Preserved):
        return node[0]
    out: list[Any] = []
    for res in node:
        if isinstance(res, list) and not isinstance(res, Preserved):
            out.extend(res)
        elif res is not MISSING:
            out.append(res)
    return _unwrap_list(out)


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
    if node is MISSING:
        return MISSING
    if not isinstance(node, list):
        return node
    return _unwrap_list(node)


def unwrap_cons(node: Any) -> Any:
    """unwrap, except that a constructor's own array survives.

    A cons value is not a sequence, so the collapse does not apply to it.
    Testing for an *empty* list is enough to tell the two apart, for the
    same reason it is in force_array_cons: every empty result that is not a
    head short-circuit has already collapsed to MISSING before it gets
    here. `[].x^($)` is `[]`, while `[1].$^($)` is `1`.
    """
    if isinstance(node, list) and not node:
        return node
    return unwrap(node)


def keep_singleton(node: Any) -> Any:
    """The collapse a live `[]` mark suppresses -- and the one it does not.

    `keepSingleton` guards only the length-1 arm of the reference's
    collapse; the length-0 arm still fires, so `a.b[]^($)` is `[1]` while
    `empty[]^($)` is undefined.
    """
    if node is None or node is MISSING:
        return MISSING
    if isinstance(node, list) and not node:
        return MISSING
    return node
