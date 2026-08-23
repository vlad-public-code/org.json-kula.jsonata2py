"""Higher-order sequence built-in functions for JSONata.

Ported from org.json_kula.jsonata_jvm.runtime.SequenceBuiltins.

All functions here are delegated from core.py's fn_map/fn_filter/etc.
overload-dispatch wrappers.
"""

from __future__ import annotations

import functools
import random
from collections.abc import Callable
from typing import Any

from ..errors import _RuntimeEvaluationError as RuntimeEvaluationError
from . import core as _core
from .values import MISSING


def fn_sort(arg: Any, key_fn: Callable[[Any], Any] | None) -> Any:
    key_fn = _core.deadline_guard(key_fn) if key_fn is not None else None
    if arg is MISSING:
        return MISSING
    if not isinstance(arg, list):
        return [arg]  # scalar: wrap in a 1-element list
    items = list(arg)
    if not items:
        return arg
    if key_fn is None:
        for elem in items:
            if isinstance(elem, (dict, list)):
                raise RuntimeEvaluationError(
                    "D3070", "$sort() cannot sort arrays of objects without a comparator function"
                )

    keys: list[Any] = [MISSING] * len(items)
    has_number = False
    has_string = False
    for i, item in enumerate(items):
        k = key_fn(item) if key_fn is not None else item
        if k is MISSING:
            # No keyFn result / undefined: skip without error (sorts last).
            keys[i] = MISSING
            continue
        if k is None:
            # JSON null is not a valid sort key.
            raise RuntimeEvaluationError(
                "T2008", "The key expression in the order-by clause must evaluate to a string or a number"
            )
        if _core.is_number(k):
            has_number = True
        elif isinstance(k, str):
            has_string = True
        else:
            raise RuntimeEvaluationError(
                "T2008", "The key expression in the order-by clause must evaluate to a string or a number"
            )
        keys[i] = k
    if has_number and has_string:
        raise RuntimeEvaluationError(
            "T2007", "The items in the order-by clause must evaluate to a single type, either all string or all number"
        )

    # Keys are already extracted and validated to be all-number or
    # all-string; a native key-sort is a direct translation of cmp's rules
    # (MISSING/None sorts last) with no per-comparison Python call.
    if has_number:

        def sort_key(i: int, _keys: list[Any] = keys) -> tuple[int, Any]:
            v = _keys[i]
            return (1, 0.0) if (v is MISSING or v is None) else (0, float(v))
    else:

        def sort_key(i: int, _keys: list[Any] = keys) -> tuple[int, Any]:
            v = _keys[i]
            return (1, "") if (v is MISSING or v is None) else (0, v)

    indices = sorted(range(len(items)), key=sort_key)
    return [items[i] for i in indices]


def fn_sort_comparator(arg: Any, comparator_fn: Callable[[Any], Any]) -> Any:
    """Sort using a 2-param comparator lambda: function($a,$b){...} returns
    true when $a should come before $b."""
    if arg is MISSING:
        return MISSING
    if not isinstance(arg, list):
        return arg
    compare = _core.deadline_guard(comparator_fn)
    items = list(arg)
    is_truthy = _core.is_truthy

    def cmp(a: Any, b: Any) -> int:
        # One comparator call per comparison, not two.
        #
        # `a < b` is asked as "does the comparator put b after a?", which
        # is a STRICT less-than: for a tied pair the comparator answers
        # false, cmp returns +1, and list.sort's merge therefore leaves
        # the two in input order. Stability is preserved by construction.
        #
        # The mirror matters. Asking it the other way round -- `1 if
        # is_truthy(compare([a, b])) else -1` -- makes `a < b` mean "not
        # (a after b)", i.e. a <= b, which is *true* for ties and swaps
        # every tied pair. Same call count, silently unstable.
        return -1 if is_truthy(compare([b, a])) else 1

    items.sort(key=functools.cmp_to_key(cmp))
    return items


def fn_sort_by_ordering_key(arg: Any, key_fn: Callable[[Any], Any], descending: bool) -> Any:
    """$sort(arr, function($a,$b){ $a.K > $b.K }) and its three mirrors.

    Exactly equivalent to fn_sort_comparator with that comparator -- same
    order, same errors, same stability -- but the key is extracted once
    per element instead of once per comparison, and when the keys turn
    out to be uniformly orderable the sort runs natively with no
    Python-level comparison at all.

    Deliberately NOT a rewrite to the ^(K) key-sort operator, which looks
    equivalent and is not: fn_sort sorts a missing key last where the
    comparator leaves it in place, and reports T2007/T2008 where the
    comparator's own gt/lt report T2009/T2010. Preserving those meant
    keeping the comparator's semantics and only changing how the work is
    scheduled.
    """
    if arg is MISSING:
        return MISSING
    if not isinstance(arg, list):
        return arg
    items = list(arg)
    if len(items) < 2:
        # The comparator would never have been invoked, so neither its
        # errors nor its key extraction may happen here either.
        return items

    key_of = _core.deadline_guard(key_fn)
    keys = [key_of(item) for item in items]

    uniform_number = True
    uniform_string = True
    for k in keys:
        kt = k.__class__
        # Exact types only: bool is an int subclass but the ordering
        # operators reject it (T2010), so it must take the slow path.
        if kt is not int and kt is not float:
            uniform_number = False
            if not uniform_string:
                break
        if kt is not str:
            uniform_string = False
            if not uniform_number:
                break

    if uniform_number:
        # float(), matching what gt/lt do to numeric operands.
        ordering: list[Any] = [float(k) for k in keys]
    elif uniform_string:
        ordering = keys
    else:
        # Missing, null, boolean, mixed or structured keys: reproduce the
        # comparator exactly, errors included. Extracting the keys once
        # still saves the repeated path navigation.
        compare = _core.lt if descending else _core.gt
        is_truthy = _core.is_truthy

        def cmp(i: int, j: int) -> int:
            return -1 if is_truthy(compare(keys[j], keys[i])) else 1

        order = sorted(range(len(items)), key=functools.cmp_to_key(cmp))
        return [items[i] for i in order]

    order = sorted(range(len(items)), key=ordering.__getitem__, reverse=descending)
    return [items[i] for i in order]


def fn_collect_pairs(source: Any, elem_fn: Callable[[Any], Any]) -> Any:
    """Produces [element, parent] pairs without flattening."""
    if source is MISSING:
        return MISSING
    parents = source if isinstance(source, list) else [source]
    pairs: list[Any] = []
    for parent in parents:
        elem = elem_fn(parent)
        if elem is MISSING:
            continue
        if isinstance(elem, list):
            for e in elem:
                if e is not MISSING:
                    pairs.append([e, parent])
        else:
            pairs.append([elem, parent])
    return pairs if pairs else MISSING


def fn_collect_triples(grandparents: Any, parent_fn: Callable[[Any], Any], elem_fn: Callable[[Any], Any]) -> Any:
    """Produces [element, parent, grandparent] triples without
    flattening."""
    if grandparents is MISSING:
        return MISSING
    gps = grandparents if isinstance(grandparents, list) else [grandparents]
    triples: list[Any] = []
    for gp in gps:
        parents = parent_fn(gp)
        if parents is MISSING:
            continue
        plist = parents if isinstance(parents, list) else [parents]
        for parent in plist:
            elem = elem_fn(parent)
            if elem is MISSING:
                continue
            if isinstance(elem, list):
                for e in elem:
                    if e is not MISSING:
                        triples.append([e, parent, gp])
            else:
                triples.append([elem, parent, gp])
    return triples if triples else MISSING


def fn_shuffle(arg: Any) -> Any:
    if arg is MISSING:
        return MISSING
    if not isinstance(arg, list):
        return arg
    items = list(arg)
    random.shuffle(items)
    return items


def fn_map(arr: Any, fn: Callable[[Any], Any]) -> Any:
    fn = _core.deadline_guard(fn)
    if arr is MISSING:
        return MISSING
    if isinstance(arr, list):
        result = [fn(elem) for elem in arr]
    else:
        result = [fn(arr)]
    return _core.unwrap(result)


def fn_filter(arr: Any, predicate: Callable[[Any], Any]) -> Any:
    # No deadline_guard here: filter_ installs one itself. Wrapping twice
    # is invisible with no timeout set (deadline_guard returns its
    # argument unchanged) but costs two extra Python frames per element
    # as soon as set_timeout() is used.
    return _core.filter_(arr, predicate)


def fn_reduce(arr: Any, fn: Callable[[Any], Any], init: Any) -> Any:
    fn = _core.deadline_guard(fn)
    if arr is MISSING:
        return init
    items = list(arr) if isinstance(arr, list) else [arr]
    if init is MISSING:
        if not items:
            return MISSING
        acc = items[0]
        start = 1
    else:
        acc = init
        start = 0
    for i in range(start, len(items)):
        acc = fn([acc, items[i], i, items])
    return acc


def fn_map_indexed(arr: Any, fn: Callable[[Any], Any]) -> Any:
    """Variant of fn_map for multi-param lambdas: passes [value, index,
    array]."""
    fn = _core.deadline_guard(fn)
    if arr is MISSING:
        return MISSING
    items = list(arr) if isinstance(arr, list) else [arr]
    result: list[Any] = []
    for i, item in enumerate(items):
        val = fn([item, i, arr])
        if val is not MISSING:
            result.append(val)
    return result


def fn_filter_indexed(arr: Any, predicate: Callable[[Any], Any]) -> Any:
    """Variant of fn_filter for multi-param lambdas: passes [value, index,
    array]."""
    predicate = _core.deadline_guard(predicate)
    if arr is MISSING:
        return MISSING
    items = list(arr) if isinstance(arr, list) else [arr]
    result: list[Any] = []
    for i, item in enumerate(items):
        if _core.is_truthy(predicate([item, i, arr])):
            result.append(item)
    return _core.unwrap(result)


def fn_single(arr: Any, predicate: Callable[[Any], Any]) -> Any:
    """Returns the single element of arr for which predicate returns
    truthy. Raises if zero or more than one element matches."""
    predicate = _core.deadline_guard(predicate)
    if arr is MISSING:
        return MISSING
    items = list(arr) if isinstance(arr, list) else [arr]
    found: Any = None
    have_found = False
    for item in items:
        if _core.is_truthy(predicate(item)):
            if have_found:
                raise RuntimeEvaluationError("D3138", "$single: more than one match found")
            found = item
            have_found = True
    if not have_found:
        raise RuntimeEvaluationError("D3139", "$single: no match found")
    return found


def fn_single_one_arg(arr: Any) -> Any:
    """1-arg $single: returns element only if exactly one exists."""
    if arr is MISSING:
        return MISSING
    items = list(arr) if isinstance(arr, list) else [arr]
    if not items:
        raise RuntimeEvaluationError("D3139", "$single: no match found")
    if len(items) > 1:
        raise RuntimeEvaluationError("D3138", "$single: more than one match found")
    return items[0]


def fn_single_indexed(arr: Any, predicate: Callable[[Any], Any]) -> Any:
    """Multi-param $single: passes [value, index, array] to the
    predicate."""
    predicate = _core.deadline_guard(predicate)
    if arr is MISSING:
        return MISSING
    items = list(arr) if isinstance(arr, list) else [arr]
    found: Any = None
    have_found = False
    for i, item in enumerate(items):
        if _core.is_truthy(predicate([item, i, arr])):
            if have_found:
                raise RuntimeEvaluationError("D3138", "$single: more than one match found")
            found = item
            have_found = True
    if not have_found:
        raise RuntimeEvaluationError("D3139", "$single: no match found")
    return found


def fn_sift(obj: Any, fn: Callable[[Any], Any]) -> Any:
    """Returns an object containing only the key/value pairs of obj for
    which fn returns truthy. Passes [value, key, object]."""
    fn = _core.deadline_guard(fn)
    if obj is MISSING or not isinstance(obj, dict):
        return MISSING
    result: dict[str, Any] = {}
    for k, v in obj.items():
        if _core.is_truthy(fn([v, k, obj])):
            result[k] = v
    return result if result else MISSING


def fn_each(obj: Any, fn: Callable[[Any], Any]) -> Any:
    fn = _core.deadline_guard(fn)
    if obj is MISSING or not isinstance(obj, dict):
        return MISSING
    result: list[Any] = []
    for k, v in obj.items():
        r = fn([v, k, obj])
        if r is not MISSING:
            result.append(r)
    return _core.unwrap(result)
