"""Tuple-mode path evaluation: `%`, `@$v` and `#$v`.

Ported from jsonata2js's `src/runtime/path.js`, which is the only one of the
three ports that models this the way the reference does. §14.3 of the
cross-port notes has the measurement that motivated the port: on a 702-case
`@`/`#` sweep jsonata2js scored 82 divergences against this port's 254 and
jsonata-jvm-compiler's 240, and the gap is architectural rather than a pile of
missing special cases.

**The model.** The reference evaluates a path carrying a binding as a stream of
*tuples* rather than of values (`evaluateTupleStep`). A tuple is a value plus
the whole tuple it came from, plus whatever `@$`/`#$` bindings are in scope:

    Tuple(v=<value>, p=<the tuple this one expanded from>, b=<bindings>)

Three things fall out of that shape which a sequence of bare values cannot
express, and which are exactly what this port was getting wrong:

* **`@$v` binds without navigating.** It records the step's own result under
  the name and then *reverts* the stream's value to the step's input, so
  `library.loans@$l.books` binds each loan but keeps navigating from
  `library`. One output tuple per (input item, step result) pair.
* **`%` walks the `p` chain**, to any depth, rather than needing the
  translator to have kept a parent variable in scope.
* **A predicate indexes within a sibling group** — the tuples sharing a
  parent — so `foo.bar[0]` is the first `bar` of each `foo`. Once the path
  is in tuple mode the reference flattens before applying stages, so the
  `global` flag switches that back to one index across the whole stream.

**Where it is used.** Only for paths that carry a binding or a `%`; everything
else keeps this port's existing value-mode codegen, which allocates no tuples
at all. That split is jsonata2js's too, and it is why tuple mode costs the
common path nothing.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from ..errors import _RuntimeEvaluationError as RuntimeEvaluationError
from .core import descendant, field, is_truthy, to_number, wildcard_context
from .values import MISSING, Preserved

__all__ = [
    "Tuple",
    "binding_value",
    "collapse_tuples",
    "cons_head",
    "cons_seed",
    "descendant_final",
    "expr_final",
    "field_final",
    "force_array_tuples",
    "group_by_tuples",
    "head_discarded",
    "matches_predicate",
    "parent_value",
    "seed",
    "seed_single",
    "seed_with_bindings",
    "sort_tuples",
    "step_context",
    "step_context_bind",
    "step_descendant",
    "step_expr",
    "step_field",
    "step_flatten",
    "step_parent",
    "step_position_bind",
    "step_predicate",
    "step_subscript",
    "step_variable",
    "step_wildcard",
    "wildcard_final",
]


class Tuple:
    """One element of a tuple stream.

    `p` is the *whole* preceding tuple rather than its value, so chasing
    `.p.p.p` reaches an ancestor at any depth -- which is what nested
    `%.%.field` needs. `g` is the sibling-grouping identity, set only on a
    tuple produced by an `@$v` revert; see `step_context_bind`.
    """

    __slots__ = ("b", "g", "p", "v")

    def __init__(
        self,
        v: Any,
        p: Tuple | None = None,
        b: dict[str, Any] | None = None,
        g: Tuple | None = None,
    ) -> None:
        self.v = v
        self.p = p
        self.b = b
        self.g = g

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"Tuple(v={self.v!r}, b={self.b!r})"


# =============================================================================
# Seeding
# =============================================================================


def seed(input_: Any) -> list[Tuple]:
    """The initial stream: an array root spreads into one tuple per element."""
    if input_ is MISSING:
        return []
    items = input_ if isinstance(input_, list) else [input_]
    return [Tuple(v) for v in items]


def seed_single(input_: Any) -> list[Tuple]:
    """`seed`, but an array root stays one opaque item.

    Standalone `*`/`**` treat an array `$` as one object-like value whose
    numeric indices are its keys, unlike a bare field step, which treats an
    array `$` as multiple implicit root items.
    """
    if input_ is MISSING:
        return []
    return [Tuple(input_)]


def seed_with_bindings(input_: Any, p: Tuple | None, b: dict[str, Any] | None) -> list[Tuple]:
    """`seed`, but inheriting a parent tuple and bindings.

    Used when a `$` source feeds a nested construct evaluated inside an
    enclosing per-element closure: `$` did not lose its parent chain or its
    bindings just because this construct re-seeds a stream from it.
    """
    if input_ is MISSING:
        return []
    items = input_ if isinstance(input_, list) else [input_]
    return [Tuple(v, p, b) for v in items]


def cons_seed(head: Any) -> list[Any]:
    """The items §1's constructor-head short-circuit hands to the next step.

    The reference's step loop iterates the head's value with
    `for (ii < input.length)`: an array is itself, a string is its
    characters, and anything without a length yields nothing at all.
    """
    if isinstance(head, list):
        return head
    if isinstance(head, str):
        return list(head)
    return []


# =============================================================================
# Navigation steps
# =============================================================================


def _expand(tuples: Iterable[Tuple], step_value_of: Callable[[Any], Any]) -> list[Tuple]:
    out: list[Tuple] = []
    for t in tuples:
        sv = step_value_of(t.v)
        if sv is MISSING:
            continue
        if isinstance(sv, list):
            out.extend(Tuple(e, t, t.b) for e in sv if e is not MISSING)
        else:
            out.append(Tuple(sv, t, t.b))
    return out


def step_field(tuples: list[Tuple], name: str, fallback_root: Any = MISSING) -> list[Tuple]:
    """`.name`, always flattening.

    `fallback_root` is supplied once the path is in tuple mode, and is looked
    up when the per-element lookup misses: that is the reference's documented
    join idiom `Employee@$e.Contact`, where `Contact` is not a field of an
    Employee and resolves against the root document instead.
    """
    out: list[Tuple] = []
    for t in tuples:
        sv = field(t.v, name)
        if sv is MISSING and fallback_root is not MISSING:
            sv = field(fallback_root, name)
        if sv is MISSING:
            continue
        if isinstance(sv, list):
            out.extend(Tuple(e, t, t.b) for e in sv if e is not MISSING)
        else:
            out.append(Tuple(sv, t, t.b))
    return out


def step_wildcard(tuples: list[Tuple]) -> list[Tuple]:
    """`*`. A tuple's value can be an array here -- it is one element of a
    step's result, not a whole sequence -- and the reference enumerates
    `Object.keys`, which for an array is its indices. That is what
    `wildcard_context` does; plain `wildcard` declines an array because in
    value mode the step has already mapped over the sequence.
    """
    return _expand(tuples, wildcard_context)


def step_descendant(tuples: list[Tuple]) -> list[Tuple]:
    return _expand(tuples, descendant)


def step_context(tuples: list[Tuple]) -> list[Tuple]:
    """`.$` -- an identity map that is still a *step*.

    It re-parents: each input tuple becomes the parent of its own output, so a
    following `#$v` indexes within that tuple's own result rather than across
    the stream. That is the whole difference between `$#$pos` (one outer item,
    indices 0..n-1) and `$.$#$pos` (n outer items, each index 0). It also
    spreads an array value, like any other step.
    """
    out: list[Tuple] = []
    for t in tuples:
        if isinstance(t.v, list):
            out.extend(Tuple(e, t, t.b) for e in t.v if e is not MISSING)
        elif t.v is not MISSING:
            out.append(Tuple(t.v, t, t.b))
    return out


def step_parent(tuples: list[Tuple]) -> list[Tuple]:
    """`%` -- each tuple is replaced by its own parent, dropped if it has none."""
    return [t.p for t in tuples if t.p is not None]


def step_expr(tuples: list[Tuple], fn: Callable[[Any, Tuple | None, Any], Any]) -> list[Tuple]:
    """An arbitrary expression as a bare step, keeping an array result nested.

    Only a syntactically bare array constructor gets this treatment; every
    other expression step flattens (see `step_flatten`).
    """
    out: list[Tuple] = []
    for t in tuples:
        v = fn(t.v, t.p, t.b)
        if v is MISSING:
            continue
        out.append(Tuple(v, t, t.b))
    return out


def step_flatten(tuples: list[Tuple], fn: Callable[[Any, Tuple | None, Any], Any]) -> list[Tuple]:
    """An expression step inside a tuple-mode path, flattening its result.

    `evaluateTupleStep` flattens any array result unconditionally -- there is
    no `cons` exception here, unlike ordinary step evaluation.
    """
    out: list[Tuple] = []
    for t in tuples:
        sv = fn(t.v, t.p, t.b)
        if isinstance(sv, list):
            out.extend(Tuple(e, t, t.b) for e in sv if e is not MISSING)
        elif sv is not MISSING:
            out.append(Tuple(sv, t, t.b))
    return out


# =============================================================================
# Bindings
# =============================================================================


def step_context_bind(tuples: list[Tuple], var_name: str) -> list[Tuple]:
    """`@$v` -- bind the step's result, and revert the stream to its input.

    `@$v` is not a navigational step in the reference: it tags the step it
    follows as a *focus* step. The bound value is that step's individual
    result (`t.v`), and the path's own position for everything after reverts
    to what it was before that step ran (`t.p`'s value). `nums@$e` is
    therefore the document once per element, not `[1,2,3]`.

    `%` does not shift for an isolated bind. But the reference's ancestor
    resolution treats a *contiguous run* of bound steps as one unit consuming
    exactly one `%` hop for the whole run, so a bind whose own source is
    itself a bind's output skips past it.

    `g` records the tuple that produced this one *before* the revert, as the
    sibling-grouping identity going forward. Reusing `p` for both would be
    wrong: `p` is shared by every sibling, which is right for grouping tuples
    still at this level but merges each sibling's own later lineage back
    together the moment they all revert to the same ancestor value.
    """
    out: list[Tuple] = []
    for t in tuples:
        b = dict(t.b) if t.b else {}
        b[var_name] = t.v
        if t.p is None:
            # Straight from `seed` (`$@$i`): the value before this step is the
            # same value, so reverting is a no-op.
            out.append(Tuple(t.v, None, b))
            continue
        continues_run = t.p.g is not None
        out.append(Tuple(t.p.v, t.p.p if continues_run else t.p, b, t.g if t.g is not None else t.p))
    return out


def step_position_bind(tuples: list[Tuple], var_name: str, global_: bool) -> list[Tuple]:
    """`#$v` -- record the 0-based index under the name.

    Scoped to each sibling group by default, matching the reference's own
    `step.index`, which is assigned during the per-outer-iteration expand and
    so restarts for every outer element. `global_` is set when this `#$v`
    instead follows a predicate or subscript on the same step, where the
    reference runs it as a stage over the already-flattened stream.
    """
    groups = [tuples] if global_ else group_by_parent(tuples)
    for siblings in groups:
        for i, t in enumerate(siblings):
            t.b = dict(t.b) if t.b else {}
            t.b[var_name] = i
    return tuples


def step_variable(
    tuples: list[Tuple], var_name: str, outer_fn: Callable[[], Any]
) -> list[Tuple]:
    """A bare `$v` step: per-tuple from the bindings, else its lexical value."""

    def resolve(_v: Any, t: Tuple) -> Any:
        if t.b is not None and var_name in t.b:
            return t.b[var_name]
        return outer_fn()

    out: list[Tuple] = []
    for t in tuples:
        sv = resolve(t.v, t)
        if sv is MISSING:
            continue
        if isinstance(sv, list):
            out.extend(Tuple(e, t, t.b) for e in sv if e is not MISSING)
        else:
            out.append(Tuple(sv, t, t.b))
    return out


def binding_value(b: dict[str, Any] | None, name: str) -> Any:
    """An `@$v`/`#$v` binding, read off the tuple it travels with."""
    if b is None:
        return MISSING
    return b.get(name, MISSING)


def parent_value(p: Tuple | None, depth: int) -> Any:
    """`%` at `depth` levels up, or MISSING if the chain is shorter.

    Depth 0 is the immediate parent, which is what a single `%` means: the
    tuple stream's `p` link already points one level up.
    """
    t = p
    for _ in range(depth):
        if t is None:
            return MISSING
        t = t.p
    return MISSING if t is None else t.v


def group_by_parent(tuples: list[Tuple]) -> list[list[Tuple]]:
    """Partitions into sibling groups, preserving first-seen order.

    The grouping identity is `g` where it is set (a tuple produced by an
    `@$v` revert) and `p` otherwise. Tuples are keyed by identity, which is
    what `Tuple`'s default hash gives.
    """
    groups: list[list[Tuple]] = []
    index: dict[int, list[Tuple]] = {}
    for t in tuples:
        anchor = t.g if t.g is not None else t.p
        key = id(anchor)
        bucket = index.get(key)
        if bucket is None:
            bucket = []
            index[key] = bucket
            groups.append(bucket)
        bucket.append(t)
    return groups


# =============================================================================
# Stages
# =============================================================================


def matches_predicate(res: Any, index: int, length: int) -> bool:
    """A predicate result, as index-or-boolean.

    A numeric result -- or an array where *every* element is numeric --
    selects by 0-based index, negatives counting from the end. Anything else
    is an ordinary truthiness test, matching the reference's all-or-nothing
    `isArrayOfNumbers` gate: one stray non-number disqualifies every number
    in the array from being read as an index.
    """
    if res is True:
        return True
    if res is False or res is MISSING:
        return False
    if isinstance(res, bool):  # pragma: no cover - covered by the two above
        return res
    if isinstance(res, (int, float)):
        idx = int(res)
        if idx < 0:
            idx = length + idx
        return idx == index
    if not isinstance(res, list):
        return is_truthy(res)
    if res and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in res):
        for v in res:
            idx = int(v)
            if idx < 0:
                idx = length + idx
            if idx == index:
                return True
        return False
    return is_truthy(res)


def step_predicate(
    tuples: list[Tuple],
    cond_fn: Callable[[Any, Tuple | None, Any, int, list[Tuple]], Any],
    global_: bool,
) -> list[Tuple]:
    """`[cond]` folded onto a step.

    Indexing is scoped to each sibling group -- `foo.bar[0]` is the first
    `bar` within each `foo` -- unless `global_` is set. Once a path uses
    `%`/`@$`/`#$` anywhere the reference expands and flattens every outer
    tuple's result *before* running the stages, so from then on a stage
    applies once across the whole stream: in `library.loans@$l.books@$b[c][1]`
    the `[1]` picks the second match overall, not per loan.
    """
    out: list[Tuple] = []
    groups = [tuples] if global_ else group_by_parent(tuples)
    for siblings in groups:
        n = len(siblings)
        for i, t in enumerate(siblings):
            if matches_predicate(cond_fn(t.v, t.p, t.b, i, siblings), i, n):
                out.append(t)
    return out


def step_subscript(
    tuples: list[Tuple],
    index_fn: Callable[[Any, Tuple | None, Any, int, list[Tuple]], Any],
    global_: bool,
) -> list[Tuple]:
    """`[n]` folded onto a step; sibling-scoped unless `global_` (see above)."""
    out: list[Tuple] = []
    groups = [tuples] if global_ else group_by_parent(tuples)
    for siblings in groups:
        n = len(siblings)
        for i, t in enumerate(siblings):
            raw = index_fn(t.v, t.p, t.b, i, siblings)
            if raw is MISSING:
                continue
            idx = int(to_number(raw))
            if idx < 0:
                idx = n + idx
            if idx == i:
                out.append(t)
    return out


# =============================================================================
# Terminal values
# =============================================================================


def _finalize_raw(raw: list[Any], keep_singleton: bool) -> Any:
    """Turns the per-tuple raw results into the path's terminal value.

    The reference's `lastStep && result.length === 1` rule: when exactly one
    input tuple produced a result, that result becomes the value verbatim, so
    `{"a":[1]}.a` is `[1]` -- the field's own array, whole -- while
    `Account.Order.Product` over many Orders still flattens into one sequence.
    """
    n = len(raw)
    if n == 0:
        return MISSING
    if n == 1:
        r = raw[0]
        if isinstance(r, list):
            # keepSingletonArray promotes a cons array rather than passing it
            # through: it is one value, not a sequence that is already an array.
            return [r] if keep_singleton and isinstance(r, Preserved) else r
        return [r] if keep_singleton else r
    if not any(isinstance(r, list) and not isinstance(r, Preserved) for r in raw):
        # Nothing to flatten -- `raw` already is the sequence.
        return raw
    seq: list[Any] = []
    for r in raw:
        if isinstance(r, list) and not isinstance(r, Preserved):
            seq.extend(e for e in r if e is not MISSING)
        else:
            seq.append(r)
    if not seq:
        return MISSING
    if len(seq) == 1 and not keep_singleton:
        return seq[0]
    return seq


def _final_value(
    tuples: list[Tuple], step_value_of: Callable[[Any], Any], keep_singleton: bool
) -> Any:
    raw: list[Any] = []
    for t in tuples:
        sv = step_value_of(t.v)
        if sv is not MISSING:
            raw.append(sv)
    return _finalize_raw(raw, keep_singleton)


def field_final(
    tuples: list[Tuple], name: str, fallback_root: Any, keep_singleton: bool
) -> Any:
    raw: list[Any] = []
    for t in tuples:
        sv = field(t.v, name)
        if sv is MISSING and fallback_root is not MISSING:
            sv = field(fallback_root, name)
        if sv is not MISSING:
            raw.append(sv)
    return _finalize_raw(raw, keep_singleton)


def wildcard_final(tuples: list[Tuple], keep_singleton: bool) -> Any:
    return _final_value(tuples, wildcard_context, keep_singleton)


def descendant_final(tuples: list[Tuple], keep_singleton: bool) -> Any:
    return _final_value(tuples, descendant, keep_singleton)


def expr_final(
    tuples: list[Tuple],
    fn: Callable[[Any, Tuple | None, Any], Any],
    keep_singleton: bool,
) -> Any:
    raw: list[Any] = []
    for t in tuples:
        r = fn(t.v, t.p, t.b)
        if r is not MISSING:
            raw.append(r)
    return _finalize_raw(raw, keep_singleton)


def collapse_tuples(tuples: list[Tuple], keep_singleton: bool) -> Any:
    """The final tuple stream, as a JSONata value."""
    if not tuples:
        return MISSING
    if len(tuples) == 1 and not keep_singleton:
        return tuples[0].v
    return [t.v for t in tuples]


def force_array_tuples(tuples: list[Tuple]) -> list[Any]:
    """`expr[]` over a tuple stream: an array even for zero or one tuples."""
    return [t.v for t in tuples]


def _append_binding(a: Any, b: Any) -> Any:
    """`fn.append` over two binding values, which is how a bucket merges them."""
    if a is MISSING:
        return b
    if b is MISSING:
        return a
    out: list[Any] = []
    for part in (a, b):
        if isinstance(part, list):
            out.extend(part)
        else:
            out.append(part)
    return out


def group_by_tuples(
    tuples: list[Tuple],
    pairs: list[tuple[Callable[[Any, Any], Any], Callable[[Any, Any], Any]]],
) -> dict[str, Any]:
    """`{...}` over a tuple stream.

    The reference runs the pairs with a frame built from each tuple, so a key
    or a value can read the `@$`/`#$` bindings that got the tuple here. When
    more than one tuple lands in the same bucket their bindings *append*
    (`reduceTupleStream`, via `fn.append`) rather than the last one winning,
    so a value sees every matching tuple's binding -- `#$i` across a repeated
    key becomes `[0,1]`.

    An empty stream still runs the pairs once with an absent context, which
    is what makes `nope{"k":1}` an object rather than nothing.
    """
    items = tuples if tuples else [Tuple(MISSING)]
    order: list[str] = []
    buckets: dict[str, tuple[int, list[Tuple]]] = {}
    for item in items:
        for pair_index, (key_fn, _) in enumerate(pairs):
            key = key_fn(item.v, item.b)
            if key is MISSING:
                continue
            if not isinstance(key, str):
                raise RuntimeEvaluationError(
                    "T1003", "The key of an object constructor must evaluate to a string"
                )
            existing = buckets.get(key)
            if existing is None:
                buckets[key] = (pair_index, [item])
                order.append(key)
            elif existing[0] != pair_index:
                raise RuntimeEvaluationError(
                    "D1009", f"Multiple key definitions evaluate to same key: '{key}'"
                )
            else:
                existing[1].append(item)

    result: dict[str, Any] = {}
    for key in order:
        pair_index, data = buckets[key]
        values = [t.v for t in data]
        context = MISSING if not values else (values[0] if len(values) == 1 else values)
        bindings = data[0].b
        for t in data[1:]:
            if not t.b:
                continue
            merged = dict(bindings) if bindings else {}
            for name, val in t.b.items():
                merged[name] = _append_binding(
                    bindings.get(name, MISSING) if bindings else MISSING, val
                )
            bindings = merged
        value = pairs[pair_index][1](context, bindings)
        if value is not MISSING:
            result[key] = value
    return result


def sort_tuples(tuples: list[Tuple], key_fn: Callable[[Any, Any], Any], descending: bool) -> list[Tuple]:
    """`^(...)` over a tuple stream, keeping the tuples themselves.

    Collapsing to values here would drop the `@$`/`#$` bindings and the parent
    chain a later step still needs -- `Employee@$e^($e.Surname).Contact` sorts
    by a *binding*, then keeps navigating.
    """
    if len(tuples) <= 1:
        return list(tuples)
    from .sequences import fn_sort

    # Sorting the *indices* rather than the tuples reuses fn_sort's key
    # validation, which is where T2007/T2008 come from.
    order = fn_sort(list(range(len(tuples))), lambda i: key_fn(tuples[i].v, tuples[i].b))
    ordered = [tuples[i] for i in order]
    if descending:
        ordered.reverse()
    return ordered


def head_discarded(_evaluated: Any, result: Any) -> Any:
    """Evaluates a constructor head for its effects and returns `result`.

    `evaluatePath`'s consarray branch computes the head and then, for a step
    carrying a focus or an index, never lets it advance the stream -- but the
    head still runs, so an error in it still surfaces.
    """
    return result


def cons_head(
    head: Any,
    rest: Callable[[list[Tuple]], Any],
    keep_singleton: bool,
    undefined_is_empty_array: bool,
    context: Any = MISSING,
) -> Any:
    """§1's constructor-head short-circuit, in tuple mode.

    The head is evaluated as a *value*, and the remaining steps run over the
    items `cons_seed` gets out of it -- so a head left a scalar by a stage
    stops the path, and an empty one is the path's whole result.
    """
    if head is MISSING:
        return [] if undefined_is_empty_array else MISSING
    items = cons_seed(head)
    if not items:
        if not undefined_is_empty_array:
            return MISSING
        return [] if isinstance(head, list) else MISSING
    # The items get the path's own input as their parent, so an `@$v` after
    # the head reverts to it rather than standing still: `[1]#$i@$e` is the
    # context, once per element of the constructor.
    parent = None if context is MISSING else Tuple(context)
    result = rest([Tuple(v, parent) for v in items])
    if keep_singleton and result is not MISSING and not isinstance(result, list):
        return [result]
    return result
