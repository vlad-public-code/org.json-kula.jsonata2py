"""Sequence scan fusion: one pass over a bound sequence instead of many.

Analytical JSONata binds a sequence once and interrogates it repeatedly::

    (
      $employees := company.departments.employees;
      $totalPayroll := $sum($employees.salary);
      $avgSalary    := $average($employees.salary);
      $topSalary    := $max($employees.salary);
      $seniorCount  := $count($employees[level = "senior"]);
      ...
    )

Each of those already compiles to a single allocation-free loop -- see
`fn_sum_field` and friends -- but they remain *twelve separate loops* over the
same nineteen elements, reading `salary` four times per element and `level`
five times.

**The unit of waste is the field read, not the loop.** A JSON object is a
`dict`; nothing can make a `dict.get` disappear except not doing it. On the
project's benchmark the block performs 551 field reads per evaluation and
roughly 230 of them are re-reads of a field already read in this evaluation.

This pass groups the operations a block performs over one sequence and emits
**one helper** that reads each distinct field once per element, feeding every
accumulator from that single read: 551 field reads per evaluation become 320,
and 1 955 function calls become 1 253.

Measured +36% end-to-end on that benchmark, as a paired A/B -- two source trees,
one per arm, alternating in separate processes within one session. Cold
compilation rises ~1.5x in exchange, because the specialised loops are more
generated source for CPython to compile; the trade repays after ~100 evaluations
of the same expression.

Design notes
------------

*Where the JVM port's conclusion does not port.* `jsonata-performance.md` §4
found that emitting the fused loop inline was -5% while emitting it as its own
method was +21%, because inlining blew the enclosing method past what HotSpot
could register-allocate well. Its porting note says to measure that on CPython
rather than assume, and measuring says the two are the same here (+10.8% helper
vs +10.5% inline for one group). A helper is used anyway, because a block body
is a list of statements built by expression-returning visitors and a helper
`def` is what that machinery already emits.

*Correctness by fallback, not by re-deriving the semantics.* The obvious risk
in a pass like this is reimplementing `$sum`'s error and coercion rules subtly
differently, and the JVM port's §4 spends most of its length on exactly that:
error ordering between aggregates, non-object elements, filter result shape.
This port sidesteps all of it. The generated loop handles only the arm every
real document takes -- a field whose value is a plain `int` or `float` -- and
records a `rescan` flag the moment it sees anything else. A slot whose flag is
set comes back as the `RESCAN` sentinel, and the use site falls back to the
**original call, at its original position**::

    v_totalPayroll = __s0[0] if __s0[0] is not RESCAN else fn_sum_field(v_employees, 'salary')

So unusual data costs one extra pass and is otherwise handled by the code that
already handles it -- including which error wins, which is then not this pass's
problem at all. Counting operations need no fallback: `fn_count_field_eq` is
total, and its per-kind comparison is reproduced exactly.

*What is deliberately left out*, all additive later and none needed for the
win: two-level paths (`$sum($orders.lines.qty)`), `$count($seq.field)`,
predicates other than `field = literal`, and filters used as values.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import TYPE_CHECKING

from ..parser.ast_nodes import (
    ArrayConstructor,
    AstNode,
    BinaryOp,
    Block,
    BooleanLiteral,
    ConditionalExpr,
    FieldRef,
    FunctionCall,
    NumberLiteral,
    ObjectConstructor,
    Parenthesized,
    PathExpr,
    PredicateExpr,
    StringLiteral,
    VariableBinding,
    VariableRef,
)
from .naming import py_string, pyvar

if TYPE_CHECKING:
    from .gen_state import GenState

#: Aggregates that reduce `$fn($seq.field)` to one number. Each maps to the
#: runtime helper the un-fused code would have called, which is also the
#: fallback when the fast arm does not apply.
_AGGREGATES = {
    "sum": "fn_sum_field",
    "average": "fn_average_field",
    "max": "fn_max_field",
    "min": "fn_min_field",
}

#: Fuse when there are at least this many operations on one sequence, or
#: exactly two that share a field. Below that the helper and its result tuple
#: cost more than the loop they remove.
_MIN_OPS = 3


@dataclass(frozen=True, slots=True)
class _Op:
    """One absorbed operation: what to accumulate, and how to redo it."""

    kind: str  # "sum" | "average" | "max" | "min" | "count_eq"
    field_name: str
    node: AstNode  # the FunctionCall, matched by identity
    literal: str = ""  # count_eq: the Python source of the expected value
    lit_kind: str = ""  # count_eq: "str" | "bool" | "num"


@dataclass(slots=True)
class _Group:
    """The operations one bound sequence carries, in source order."""

    var: str
    first_stmt: int
    ops: list[_Op] = dc_field(default_factory=list)

    def worth_fusing(self) -> bool:
        if len(self.ops) >= _MIN_OPS:
            return True
        # Exactly two operations pay for themselves only by sharing a read.
        return len(self.ops) == 2 and self.ops[0].field_name == self.ops[1].field_name


@dataclass(slots=True)
class ScanPlan:
    """A block's fusion plan: which groups to emit, and where."""

    groups: list[_Group]
    #: id(FunctionCall) -> the Python expression that replaces it. Filled in
    #: as each group is emitted, so a lookup before that point misses and the
    #: call compiles normally.
    slots: dict[int, str] = dc_field(default_factory=dict)
    #: Keeps the matched nodes alive, so an id() can never be recycled onto a
    #: different node while this plan is in use.
    _pinned: list[AstNode] = dc_field(default_factory=list)

    def slot_for(self, node: AstNode) -> str | None:
        return self.slots.get(id(node))

    def emit_for_statement(self, index: int, state: GenState) -> list[str]:
        """Emits the scan calls whose first use is statement `index`."""
        lines: list[str] = []
        for group in self.groups:
            if group.first_stmt == index:
                lines.append(_emit_group(group, self, state))
        return lines


def plan(exprs: list[AstNode], state: GenState) -> ScanPlan | None:
    """Plans fusion for one block, before any of its statements is compiled.

    Returns None when nothing is worth fusing, which is the common case and
    costs one walk of the block.
    """
    bound_at = _names_bound_exactly_once(exprs)
    if not bound_at:
        return None

    groups: dict[str, _Group] = {}
    pinned: list[AstNode] = []
    for index, expr in enumerate(exprs):
        value = expr.value if isinstance(expr, VariableBinding) else expr
        for var, op in _collect(value, state, bound_at, index):
            group = groups.get(var)
            if group is None:
                group = groups[var] = _Group(var=var, first_stmt=index)
            group.ops.append(op)
            pinned.append(op.node)

    kept = [g for g in groups.values() if g.worth_fusing()]
    if not kept:
        return None
    return ScanPlan(groups=kept, _pinned=pinned)


# =============================================================================
# Planning
# =============================================================================


def _names_bound_exactly_once(exprs: list[AstNode]) -> dict[str, int]:
    """Names this block binds exactly once, mapped to their statement index.

    A name bound twice is excluded outright: the scan is hoisted to the
    group's first use and would then read a different value than a later
    statement did.
    """
    seen: dict[str, int] = {}
    rebound: set[str] = set()
    for index, expr in enumerate(exprs):
        node = expr
        while isinstance(node, VariableBinding):
            if node.name in seen:
                rebound.add(node.name)
            else:
                seen[node.name] = index
            node = node.value
    for name in rebound:
        del seen[name]
    return seen


def _collect(node: AstNode, state: GenState, bound_at: dict[str, int], index: int) -> list[tuple[str, _Op]]:
    """Absorbable operations in positions the block evaluates unconditionally.

    A whitelist, not a blacklist. Hoisting an operation out of a position that
    might not be evaluated -- a lambda body, an untaken conditional branch, the
    right of `and`/`or` -- would run it on data the expression never looks at,
    and failing open here means wrong answers rather than lost speed. An
    unrecognised node type is simply not descended into.

    A matched node is absorbed **whole** and not descended into, which is what
    stops `$count($s[g = 'a'])` collecting the count and then its own inner
    filter as two separate operations.
    """
    out: list[tuple[str, _Op]] = []
    _walk(node, state, bound_at, index, out)
    return out


def _walk(node: AstNode, state: GenState, bound_at: dict[str, int], index: int, out: list[tuple[str, _Op]]) -> None:
    matched = _match(node, state, bound_at, index)
    if matched is not None:
        out.append(matched)
        return

    if isinstance(node, FunctionCall):
        # Unmatched, but its arguments are still certain to be evaluated:
        # `$round($average($s.f), 2)`.
        for arg in node.args:
            _walk(arg, state, bound_at, index, out)
        return
    if isinstance(node, PredicateExpr):
        # The predicate runs per element, so it is not an unconditional
        # position of the enclosing block; the source is.
        _walk(node.source, state, bound_at, index, out)
        return
    if isinstance(node, Parenthesized):
        _walk(node.inner, state, bound_at, index, out)
        return
    if isinstance(node, Block):
        # A nested block plans its own fusion; its statements are not this
        # block's to hoist.
        return
    if isinstance(node, BinaryOp):
        _walk(node.left, state, bound_at, index, out)
        if node.op not in ("and", "or"):  # only the left operand is certain
            _walk(node.right, state, bound_at, index, out)
        return
    if isinstance(node, ConditionalExpr):
        _walk(node.condition, state, bound_at, index, out)  # branches are not
        return
    if isinstance(node, ArrayConstructor):
        for element in node.elements:
            _walk(element, state, bound_at, index, out)
        return
    if isinstance(node, ObjectConstructor):
        for pair in node.pairs:
            _walk(pair.key, state, bound_at, index, out)
            _walk(pair.value, state, bound_at, index, out)
        return
    if isinstance(node, VariableBinding):
        _walk(node.value, state, bound_at, index, out)
        return


def _match(node: AstNode, state: GenState, bound_at: dict[str, int], index: int) -> tuple[str, _Op] | None:
    """Recognises one absorbable operation, or None."""
    if isinstance(node, PredicateExpr):
        return _match_filter(node, state, bound_at, index)
    if not isinstance(node, FunctionCall) or not node.is_variable or len(node.args) != 1:
        return None
    # A shadowed name is no longer the built-in. `bound_at` covers this block;
    # `is_local` covers every enclosing scope.
    if node.name in bound_at or state.is_local(node.name):
        return None
    arg = node.args[0]

    if node.name in _AGGREGATES:
        if not isinstance(arg, PathExpr) or len(arg.steps) != 2:
            return None
        source, step = arg.steps
        if not isinstance(step, FieldRef):
            return None
        var = _sequence_var(source, bound_at, index)
        if var is None or state.contains_parent_step(arg):
            return None
        return var, _Op(kind=node.name, field_name=step.name, node=node)

    if node.name == "count":
        if not isinstance(arg, PredicateExpr):
            return None
        matched = field_eq_literal(arg.predicate)
        if matched is None or state.contains_parent_step(arg.predicate):
            return None
        var = _sequence_var(arg.source, bound_at, index)
        if var is None:
            return None
        name, lit_source, lit_kind = matched
        return var, _Op(kind="count_eq", field_name=name, node=node, literal=lit_source, lit_kind=lit_kind)

    return None


def _match_filter(node: PredicateExpr, state: GenState, bound_at: dict[str, int], index: int) -> tuple[str, _Op] | None:
    """`$seq[field = <literal>]` used as a value."""
    matched = field_eq_literal(node.predicate)
    if matched is None or state.contains_parent_step(node.predicate):
        return None
    var = _sequence_var(node.source, bound_at, index)
    if var is None:
        return None
    name, lit_source, lit_kind = matched
    return var, _Op(kind="filter_eq", field_name=name, node=node, literal=lit_source, lit_kind=lit_kind)


def field_eq_literal(predicate: AstNode) -> tuple[str, str, str] | None:
    """`field = <literal>` as (field name, Python source, literal kind).

    Shared by the fusion planner and by the ordinary predicate code
    generator, so the two agree by construction on which predicates have a
    monomorphized form -- which matters because that form is also the
    fallback a fused slot redirects to.
    """
    if not (isinstance(predicate, BinaryOp) and predicate.op == "=" and isinstance(predicate.left, FieldRef)):
        return None
    literal = _literal_source(predicate.right)
    if literal is None:
        return None
    return predicate.left.name, literal[0], literal[1]


def _sequence_var(source: AstNode, bound_at: dict[str, int], index: int) -> str | None:
    """The bound name this operation scans, if it is one this block owns and
    bound *before* the statement now using it."""
    if not isinstance(source, VariableRef):
        return None
    bound = bound_at.get(source.name)
    if bound is None or bound >= index:
        return None
    return source.name


def _literal_source(node: AstNode) -> tuple[str, str] | None:
    """The Python source and kind of a comparison literal, or None for kinds
    whose comparison this pass does not reproduce."""
    if isinstance(node, StringLiteral):
        return py_string(node.value), "str"
    if isinstance(node, BooleanLiteral):
        return ("True" if node.value else "False"), "bool"
    if isinstance(node, NumberLiteral):
        value = node.value
        as_int = int(value)
        return (repr(as_int) if float(as_int) == value else repr(value)), "num"
    return None


# =============================================================================
# Emission
# =============================================================================


def _emit_group(group: _Group, plan_: ScanPlan, state: GenState) -> str:
    """Emits the helper for one group and returns the call statement."""
    uid = state.next_id()
    seq_var = pyvar(group.var)

    # One accumulator set per operation; operations are grouped by field so
    # each distinct field is read exactly once per element.
    inits: list[str] = []
    body: list[str] = []
    results: list[str] = []

    by_field: dict[str, list[_Op]] = {}
    for op in group.ops:
        by_field.setdefault(op.field_name, []).append(op)

    slot_of: dict[int, int] = {}
    for slot, op in enumerate(group.ops):
        slot_of[id(op)] = slot

    # A rescan flag per field that carries an operation with a fast arm --
    # the numeric aggregates and the filters. Counting needs none: its
    # per-kind comparison is reproduced in full, so it cannot fall short.
    rescan_of: dict[str, str] = {}
    for field_name, ops in by_field.items():
        if any(op.kind in _AGGREGATES or op.kind == "filter_eq" for op in ops):
            rescan_of[field_name] = f"_rs{uid}_{len(rescan_of)}"

    for name in rescan_of.values():
        inits.append(f"{name} = False")

    for slot, op in enumerate(group.ops):
        inits.extend(_accumulator_inits(op, uid, slot))

    val_var = f"_e{uid}"
    for field_index, (field_name, ops) in enumerate(by_field.items()):
        fv = f"_f{uid}_{field_index}"
        body.append(f"{fv} = {val_var}.get({py_string(field_name)}, MISSING)")
        numeric = [op for op in ops if op.kind in _AGGREGATES]
        if numeric:
            body.append(f"if {fv}.__class__ is int or {fv}.__class__ is float:")
            for op in numeric:
                body.extend(f"    {line}" for line in _fold(op, uid, slot_of[id(op)], fv))
            body.append(f"elif {fv} is not MISSING:")
            body.append(f"    {rescan_of[field_name]} = True")
        counts = [op for op in ops if op.kind == "count_eq"]
        if counts:
            body.extend(_count_folds(counts, uid, slot_of, fv))
        filters = [op for op in ops if op.kind == "filter_eq"]
        if filters:
            body.extend(_filter_folds(filters, uid, slot_of, fv, val_var, rescan_of[field_name]))

    for slot, op in enumerate(group.ops):
        results.append(_result_expr(op, uid, slot, rescan_of.get(op.field_name)))

    seq_param = f"_sq{uid}"
    lines: list[str] = [*inits]
    lines.append(f"for {val_var} in ({seq_param} if {seq_param}.__class__ is list else ({seq_param},)):")
    lines.append(f"    if {val_var}.__class__ is not dict and not isinstance({val_var}, dict):")
    lines.append("        continue")
    lines.extend(f"    {line}" for line in body)
    lines.append(f"return ({', '.join(results)},)")

    helper = state.new_helper_def("scan", [seq_param], lines)
    result_var = f"_sv{uid}"

    for slot, op in enumerate(group.ops):
        access = f"{result_var}[{slot}]"
        if op.kind in _AGGREGATES:
            fallback = f"{_AGGREGATES[op.kind]}({seq_var}, {py_string(op.field_name)})"
        elif op.kind == "filter_eq":
            fallback = f"filter_field_eq({seq_var}, {py_string(op.field_name)}, {op.literal})"
        else:
            plan_.slots[id(op.node)] = access
            continue
        plan_.slots[id(op.node)] = f"({access} if {access} is not RESCAN else {fallback})"

    return f"{result_var} = {helper}({seq_var})"


def _accumulator_inits(op: _Op, uid: int, slot: int) -> list[str]:
    p = f"_a{uid}_{slot}"
    if op.kind in ("sum", "average"):
        return [f"{p}t = 0.0; {p}n = 0"]
    if op.kind in ("max", "min"):
        return [f"{p}b = None"]
    if op.kind == "filter_eq":
        return [f"{p}l = None"]
    return [f"{p}c = 0"]


def _fold(op: _Op, uid: int, slot: int, fv: str) -> list[str]:
    """The fast arm: what to do with a plain int/float field value."""
    p = f"_a{uid}_{slot}"
    if op.kind in ("sum", "average"):
        return [f"{p}t += {fv}", f"{p}n += 1"]
    if op.kind == "max":
        return [f"if {p}b is None or {fv} > {p}b: {p}b = {fv}"]
    return [f"if {p}b is None or {fv} < {p}b: {p}b = {fv}"]


def _count_folds(ops: list[_Op], uid: int, slot_of: dict[int, int], fv: str) -> list[str]:
    """`$count($seq[field = literal])` for every count on one field.

    Reproduces fn_count_field_eq's per-kind comparison exactly, but hoists the
    kind test out of the individual comparisons: the benchmark counts `level`
    against five different strings, and paying one `isinstance` for all five
    instead of one each is most of what fusing this operation buys.

    The comparisons chain with `elif` when the literals are pairwise distinct,
    since at most one can then match. Repeated literals fall back to
    independent `if`s -- `$count($e[l="a"])` twice must increment both.
    """
    lines: list[str] = []
    for kind in ("str", "bool", "num"):
        same = [op for op in ops if op.lit_kind == kind]
        if not same:
            continue
        distinct = len({op.literal for op in same}) == len(same)
        counters = [f"_a{uid}_{slot_of[id(op)]}c" for op in same]

        if kind == "bool":
            # `bool` cannot be subclassed, so identity is exactly equality.
            for op, counter in zip(same, counters, strict=True):
                lines.append(f"if {fv} is {op.literal}:")
                lines.append(f"    {counter} += 1")
            continue

        if kind == "str":
            guard = f"if {fv}.__class__ is str or isinstance({fv}, str):"
            body = [(f"{fv} == {op.literal}", counter) for op, counter in zip(same, counters, strict=True)]
            lines.append(guard)
            lines.extend(f"    {line}" for line in _chain(body, distinct))
            continue

        # Numbers: the exact-type arm covers every real document; the general
        # arm below it keeps int/float subclasses comparing as they did.
        lines.append(f"if {fv}.__class__ is int or {fv}.__class__ is float:")
        fast = [(f"{fv} == {op.literal}", counter) for op, counter in zip(same, counters, strict=True)]
        lines.extend(f"    {line}" for line in _chain(fast, distinct))
        lines.append(f"elif is_number({fv}):")
        slow = [(f"float({fv}) == {float(op.literal)!r}", counter) for op, counter in zip(same, counters, strict=True)]
        lines.extend(f"    {line}" for line in _chain(slow, distinct))
    return lines


def _filter_folds(ops: list[_Op], uid: int, slot_of: dict[int, int], fv: str, elem: str, rescan: str) -> list[str]:
    """`$seq[field = literal]` for every filter on one field.

    Only the exact-type arm is reproduced; anything else sets the field's
    rescan flag and the use site redoes that filter with filter_field_eq. So
    the shapes `field()` would navigate rather than read -- a nested list, a
    null -- are handled by the code that already handles them.
    """
    by_kind: dict[str, list[_Op]] = {}
    for op in ops:
        by_kind.setdefault(op.lit_kind, []).append(op)
    lines: list[str] = []
    guards = {
        "str": f"{fv}.__class__ is str",
        "bool": f"{fv}.__class__ is bool",
        "num": f"{fv}.__class__ is int or {fv}.__class__ is float",
    }
    for kind, same in by_kind.items():
        lines.append(f"if {guards[kind]}:")
        tests = [(f"{fv} == {op.literal}", f"_a{uid}_{slot_of[id(op)]}l") for op in same]
        distinct = len({op.literal for op in same}) == len(same)
        for i, (test, acc) in enumerate(tests):
            keyword = "elif" if distinct and i else "if"
            lines.append(f"    {keyword} {test}:")
            # One line each: generated source size is compile() time, and the
            # pass already costs the caller a slower compile.
            lines.append(f"        if {acc} is None: {acc} = [{elem}]")
            lines.append(f"        else: {acc}.append({elem})")
        lines.append(f"elif {fv} is not MISSING:")
        lines.append(f"    {rescan} = True")
    return lines


def _chain(tests: list[tuple[str, str]], distinct: bool) -> list[str]:
    lines: list[str] = []
    for i, (test, counter) in enumerate(tests):
        keyword = "elif" if distinct and i else "if"
        lines.append(f"{keyword} {test}: {counter} += 1")
    return lines


def _result_expr(op: _Op, uid: int, slot: int, rescan: str | None) -> str:
    p = f"_a{uid}_{slot}"
    if op.kind == "sum":
        value = f"(num_node({p}t) if {p}n else MISSING)"
    elif op.kind == "average":
        value = f"(num_node({p}t / {p}n) if {p}n else MISSING)"
    elif op.kind in ("max", "min"):
        value = f"(num_node(float({p}b)) if {p}b is not None else MISSING)"
    elif op.kind == "filter_eq":
        # JSONata returns nothing for no matches, the element itself for
        # exactly one, and an array only for several. Collecting into a list
        # unconditionally would be slower than not fusing at all: the
        # un-fused path allocates nothing for 0-1 matches, and most filters
        # select one element.
        value = f"(MISSING if {p}l is None else ({p}l[0] if len({p}l) == 1 else {p}l))"
    else:
        return f"{p}c"
    return f"(RESCAN if {rescan} else {value})"
