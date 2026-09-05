"""Emits a path that carries an `@$v` or `#$v` binding as a tuple stream.

The counterpart to `path_codegen`, which stays the default: a path with no
binding never allocates a tuple. This module is reached only from
`path_codegen.visit_path_expr`, and only when `_has_any_binding` says so --
the same value-mode/tuple-mode split jsonata2js makes, and the reason tuple
mode costs the common path nothing.

Ported from jsonata2js's `translator.js#compilePathSteps`/`compilePathStep`.
The one structural difference is that jsonata2js emits statements (`let tp0 =
...`) while this port composes expressions, so a step here wraps the previous
step's expression instead of naming it. `runtime/tuples.py` documents the
model itself.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..parser.ast_nodes import (
    ArrayConstructor,
    ArraySubscript,
    AstNode,
    ContextBinding,
    ContextRef,
    DescendantStep,
    FieldRef,
    ForceArray,
    GroupByExpr,
    ParentStep,
    PathExpr,
    PositionBinding,
    PredicateExpr,
    RootRef,
    SortExpr,
    VariableRef,
    WildcardStep,
    accept,
    consarray_path_head,
    is_path_node,
)
from .naming import py_string

if TYPE_CHECKING:
    from .gen_ctx import GenCtx
    from .translator import Translator

#: Node types the reference treats as steps of the enclosing path rather than
#: as a value expression seeding it.
_STEP_TYPES: tuple[type[AstNode], ...] = (
    ForceArray,
    PathExpr,
    FieldRef,
    WildcardStep,
    DescendantStep,
    ParentStep,
    ContextBinding,
    PositionBinding,
    PredicateExpr,
    ArraySubscript,
    ForceArray,
    SortExpr,
    VariableRef,
    ContextRef,
)

#: How deep a `%` chain the callbacks are given access to. The reference
#: resolves ancestry while building the AST, so an expression that reaches
#: past its parents is S0217 rather than an unbounded walk; this is a bound on
#: what any real expression asks for, and beyond it `parent_value` yields
#: nothing rather than raising.
_PARENT_DEPTH = 6


def path_needs_tuples(steps: list[AstNode]) -> bool:
    """True if this path must be evaluated as a tuple stream.

    Only a binding forces it. `%` alone stays on the value-mode compiler,
    which tracks parents at compile time and is exact for the shapes that
    reach it -- moving it here would be a rewrite with nothing to show for
    it.

    A binding can sit *inside* a stage rather than beside it: the parser
    folds `nums#$i[0]` to `ArraySubscript(PositionBinding, 0)` as one step,
    so this has to look through a stage's source as well as along the steps.
    """
    return any(_carries_binding(s) for s in steps)


def _carries_binding(node: AstNode) -> bool:
    if isinstance(node, (ContextBinding, PositionBinding)):
        return True
    if isinstance(node, (PredicateExpr, ArraySubscript, SortExpr)):
        return _carries_binding(node.source)
    if isinstance(node, PathExpr):
        return any(_carries_binding(s) for s in node.steps)
    return False


#: Stages that can be folded onto a binding by the parser.
_STAGES: tuple[type[AstNode], ...] = (PredicateExpr, ArraySubscript, SortExpr, ForceArray)


def _is_navigational(step: AstNode) -> bool:
    """True for a step that actually moves, as opposed to a binding or a
    stage hanging off the step before it."""
    return not isinstance(step, (ContextBinding, PositionBinding, *_STAGES))


def _stage_over_binding(step: AstNode) -> AstNode | None:
    """The binding under a step's stages, when the step is nothing else."""
    if not isinstance(step, (PredicateExpr, ArraySubscript, SortExpr, ForceArray)):
        return None
    inner: AstNode = step.source
    while isinstance(inner, (PredicateExpr, ArraySubscript, SortExpr, ForceArray)):
        inner = inner.source
    return inner if isinstance(inner, ContextBinding) else None


def _replace_binding_source(step: AstNode, head: AstNode) -> AstNode:
    """Rebuilds a stage chain with `head` where its binding was."""
    if isinstance(step, ContextBinding):
        return head
    if isinstance(step, PredicateExpr):
        return PredicateExpr(_replace_binding_source(step.source, head), step.predicate)
    if isinstance(step, ArraySubscript):
        return ArraySubscript(_replace_binding_source(step.source, head), step.index)
    if isinstance(step, SortExpr):
        return SortExpr(_replace_binding_source(step.source, head), step.keys)
    assert isinstance(step, ForceArray)
    return ForceArray(_replace_binding_source(step.source, head), step.on_sequence)


def _bound_names(steps: list[AstNode], up_to: int) -> list[str]:
    out: list[str] = []
    for s in steps[:up_to]:
        if isinstance(s, (ContextBinding, PositionBinding)):
            out.append(s.var_name)
    return out


class _Emitter:
    """One tuple-mode path. Holds the per-path naming state."""

    def __init__(self, t: Translator, steps: list[AstNode], ctx: GenCtx) -> None:
        self.t = t
        self.steps = steps
        self.ctx = ctx
        # Set once a `%`, `@$` or `#$` has been seen: from then on the
        # reference flattens before applying a stage, so a stage indexes
        # across the whole stream rather than within a sibling group.
        self.tuple_stream_started = False
        # Set by a stage, cleared by a navigation step: a `#$v` that follows
        # a stage on the same step is a stage itself and indexes globally.
        self.step_has_stage = False
        self.active_bindings: list[str] = []

    # -- callbacks ---------------------------------------------------------

    def _callback(self, node: AstNode, staged: bool) -> str:
        """`lambda _v, _p, _b[, _i, _sib]: <node>` with the tuple in scope.

        Inside the body `$` is the tuple's value, `%` walks its parent chain,
        and every name bound so far resolves from the tuple's bindings rather
        than from a Python local -- which is the whole point of the tuple
        stream: the binding travels with the value.
        """
        state = self.ctx.state
        vid = state.next_id()
        v_var, p_var, b_var = f"_tv{vid}", f"_tp{vid}", f"_tb{vid}"
        parents = tuple(
            f"parent_value({p_var}, {d})" for d in reversed(range(_PARENT_DEPTH))
        )
        state.push_scope()
        try:
            for name in self.active_bindings:
                state.add_local_var_with_alias(name, f"binding_value({b_var}, {py_string(name)})")
            inner = self.ctx.with_ctx(v_var).with_parents(parents)
            body = accept(node, self.t, inner)
        finally:
            state.pop_scope()
        params = f"{v_var}, {p_var}, {b_var}"
        if staged:
            params += f", _ti{vid}, _ts{vid}"
        return f"lambda {params}: {body}"

    # -- steps -------------------------------------------------------------

    def _fallback_root(self) -> str:
        """The reference's `Employee@$e.Contact` idiom.

        Once a binding is active a field step that misses per-element is
        looked up on the root document instead, which is what makes the
        documented join form resolve at all.
        """
        return f", {self.ctx.root_var}" if self.active_bindings else ""

    def _step(self, step: AstNode, cur: str, is_first: bool) -> str:
        if isinstance(step, FieldRef):
            self.step_has_stage = False
            return f"step_field({cur}, {py_string(step.name)}{self._fallback_root()})"
        if isinstance(step, WildcardStep):
            self.step_has_stage = False
            return f"step_wildcard({cur})"
        if isinstance(step, DescendantStep):
            self.step_has_stage = False
            return f"step_descendant({cur})"
        if isinstance(step, ParentStep):
            self.tuple_stream_started = True
            return f"step_parent({cur})"
        if isinstance(step, PathExpr):
            # A sub-path used as a step -- `a.b[]` parses the `[]` around the
            # whole path, so `@$v` after it sees one step here. Chaining its
            # steps keeps every one of them its own parent, which is what the
            # revert then lands on.
            expr = cur
            for sub in step.steps:
                expr = self._step(sub, expr, False)
            return expr
        if isinstance(step, ForceArray):
            # `a[]@$e`: the `[]` is a mark read once at the end of the path
            # (the caller's `keep`), not a step of its own -- so compile what
            # it wraps and let the mark ride.
            return self._step(step.source, cur, is_first)
        if isinstance(step, ContextRef):
            self.step_has_stage = False
            return f"step_context({cur})"
        if isinstance(step, PositionBinding):
            # The first `#$v` for a navigational step is scoped per sibling
            # group; one that follows a stage on the same step is a stage
            # itself, and indexes across the whole filtered stream.
            global_ = self.step_has_stage
            self.active_bindings.append(step.var_name)
            self.tuple_stream_started = True
            return f"step_position_bind({cur}, {py_string(step.var_name)}, {global_!r})"
        if isinstance(step, ContextBinding):
            self.active_bindings.append(step.var_name)
            self.tuple_stream_started = True
            return f"step_context_bind({cur}, {py_string(step.var_name)})"
        if isinstance(step, VariableRef):
            outer = accept(step, self.t, self.ctx)
            return f"step_variable({cur}, {py_string(step.name)}, lambda: {outer})"
        if isinstance(step, PredicateExpr):
            mid = self._fold_source(step.source, cur, is_first)
            global_ = bool(self.active_bindings) or self.tuple_stream_started
            cb = self._callback(step.predicate, staged=True)
            self.step_has_stage = True
            return f"step_predicate({mid}, {cb}, {global_!r})"
        if isinstance(step, ArraySubscript):
            mid = self._fold_source(step.source, cur, is_first)
            global_ = bool(self.active_bindings) or self.tuple_stream_started
            cb = self._callback(step.index, staged=True)
            self.step_has_stage = True
            return f"step_subscript({mid}, {cb}, {global_!r})"
        if isinstance(step, GroupByExpr) and isinstance(step.source, ContextRef):
            return f"group_by_tuples({cur}, [{self._pairs(step)}])"
        if isinstance(step, SortExpr):
            return self._sort(step, self._fold_source(step.source, cur, is_first))
        # An arbitrary expression as a bare step. A tuple-mode path runs every
        # step through the reference's `evaluateTupleStep`, which flattens an
        # array result unconditionally -- there is no array-constructor
        # exception here, which is why `Employee@$e.(Contact)` works.
        self.step_has_stage = False
        cb = self._callback(step, staged=False)
        return f"step_flatten({cur}, {cb})"

    def _pair_callback(self, node: AstNode) -> str:
        """`lambda _v, _b: <node>` -- a group-by pair sees the bucket's context
        and its merged bindings, but no parent chain (the reference builds the
        frame from the tuple, which carries bindings only)."""
        state = self.ctx.state
        vid = state.next_id()
        v_var, b_var = f"_gv{vid}", f"_gb{vid}"
        state.push_scope()
        try:
            for name in self.active_bindings:
                state.add_local_var_with_alias(name, f"binding_value({b_var}, {py_string(name)})")
            body = accept(node, self.t, self.ctx.with_ctx(v_var))
        finally:
            state.pop_scope()
        return f"lambda {v_var}, {b_var}: {body}"

    def _pairs(self, step: GroupByExpr) -> str:
        return ", ".join(
            f"({self._pair_callback(p.key)}, {self._pair_callback(p.value)})" for p in step.pairs
        )

    def _sort(self, step: SortExpr, cur: str) -> str:
        """`^(...)` over the stream, keeping tuples.

        Collapsing here would drop the bindings and parent chain a later step
        still needs -- `Employee@$e^($e.Surname).Contact` sorts by a binding
        and then keeps navigating.
        """
        expr = cur
        for sk in reversed(step.keys):
            expr = f"sort_tuples({expr}, {self._pair_callback(sk.key)}, {sk.descending!r})"
        return expr

    def _fold_source(self, source: AstNode, cur: str, is_first: bool) -> str:
        """A stage's `.source`: identity, more steps, or a value to seed from."""
        if isinstance(source, ContextRef):
            return cur
        if isinstance(source, PathExpr):
            expr = cur
            for s in source.steps:
                expr = self._step(s, expr, False)
            return expr
        if isinstance(source, _STEP_TYPES):
            return self._step(source, cur, is_first)
        if is_first:
            # The stage *is* the path's first step (`$v[0]`): its source
            # establishes the sequence, evaluated once against the context.
            return f"seed({accept(source, self.t, self.ctx)})"
        cb = self._callback(source, staged=False)
        helper = "step_expr" if isinstance(source, ArrayConstructor) else "step_flatten"
        return f"{helper}({cur}, {cb})"

    # -- terminal ----------------------------------------------------------

    def _terminal(self, step: AstNode, cur: str, keep: bool) -> str | None:
        """The last step, as a value rather than a stream.

        The reference's `lastStep && result.length === 1` rule: when exactly
        one input tuple produced a result, that result is the path's value
        verbatim -- so `{"a":[1]}.a` is `[1]` while a many-tuple step still
        flattens. Only a step that reads a value can take this; a binding or
        a stage produces a stream and collapses instead.
        """
        if isinstance(step, FieldRef):
            return f"field_final({cur}, {py_string(step.name)}, {self.ctx.root_var if self.active_bindings else 'MISSING'}, {keep!r})"
        if isinstance(step, WildcardStep):
            return f"wildcard_final({cur}, {keep!r})"
        if isinstance(step, DescendantStep):
            return f"descendant_final({cur}, {keep!r})"
        if isinstance(step, GroupByExpr) and isinstance(step.source, ContextRef):
            # A group-by *is* the value: the reference replaces the whole
            # result with its object, so there is no stream left to collapse.
            return f"group_by_tuples({cur}, [{self._pairs(step)}])"
        return None

    # -- driver ------------------------------------------------------------

    def emit(self, keep: bool) -> str:
        steps = self.steps
        i = 0
        head = steps[0]
        if not is_path_node(head) and len(steps) == 2 and _stage_over_binding(steps[1]) is not None:
            # `1@$e[0]`, `(nums)@$e[0]`: only the `.` production makes a path,
            # so a *stage* written after the binding is a stage on the head's
            # own value, not a path step. Replace the binding with the head
            # and compile it as the ordinary expression it is.
            rebuilt = _replace_binding_source(steps[1], head)
            return accept(rebuilt, self.t, self.ctx)
        if all(isinstance(st, ContextBinding) for st in steps[1:]) and not is_path_node(head):
            # `[1]@$e`, `"z"@$e`, `$sum(nums)@$e`: `@` on a non-path left side
            # builds no path at all -- the focus hangs off the node and the
            # value is simply the node's own. Only a following *step* makes a
            # path, which is the branch below.
            return accept(head, self.t, self.ctx)
        if isinstance(steps[1] if len(steps) > 1 else None, (ContextBinding, PositionBinding)) and (
            consarray_path_head(head) is not None
        ):
            # A binding written on a constructor head belongs to the head,
            # and the head is evaluated as a *value* and discarded -- so the
            # rest of the path restarts from the path's own input with no
            # bindings in scope. `[1,2]#$i@$e.$` is the context once, and
            # `[1]#$i.$i` is undefined because `$i` never reaches here.
            after = 1
            while after < len(steps) and isinstance(steps[after], (ContextBinding, PositionBinding)):
                after += 1
            if after >= len(steps):
                # Nothing follows the bindings, so the head's own value is
                # the result -- the ordinary consarray handling below.
                pass
            elif any(isinstance(st, ContextBinding) for st in steps[1:after]):
                # A *focus* on the head stops it advancing the stream:
                # `evaluatePath` leaves `inputSequence` alone for a step
                # carrying one, so the rest restarts from the path's input.
                head_expr = accept(head, self.t, self.ctx)
                rest = self._rest(steps, after, f"seed({self.ctx.ctx_var})", keep)
                return f"head_discarded({head_expr}, {rest})"
            else:
                # Only an index: the head's value *does* advance the stream,
                # but its bindings still never reach the rest -- the consarray
                # branch evaluates the head outside the tuple machinery, so no
                # bindings are ever made. `[1]#$i.$` is `1`, `[1]#$i.$i` is
                # undefined.
                head_var = f"_ch{self.ctx.state.next_id()}"
                rest = self._rest(steps, after, head_var, keep)
                head_expr = accept(head, self.t, self.ctx)
                keeps_empty = any(_is_navigational(st) for st in steps[after:])
                return (
                    f"cons_head({head_expr}, lambda {head_var}: {rest}, "
                    f"{keep!r}, {keeps_empty!r}, {self.ctx.ctx_var})"
                )
        if isinstance(steps[1] if len(steps) > 1 else None, ContextBinding):
            # A focus on the head means the head's own result never advances
            # the stream: `evaluatePath` leaves `inputSequence` alone for a
            # step carrying one. So the stream is seeded from the context and
            # the head is a step over it.
            expr = f"seed({self.ctx.ctx_var})"
            return self._rest(steps, 0, expr, keep)
        if consarray_path_head(head) is not None:
            # Section 1's short-circuit: a flagged constructor head is
            # evaluated as a *value*, and the remaining steps run over the
            # items it yields -- so an empty constructor is the path's whole
            # result and the rest never runs. `[].x@$e` is `[]`.
            head_var = f"_ch{self.ctx.state.next_id()}"
            rest = self._rest(steps, 1, head_var, keep)
            head_expr = accept(head, self.t, self.ctx)
            # The empty array a constructor head short-circuits to is the
            # path's result only when a real *step* follows it -- `[].x@$e`
            # is `[]`. With nothing after the head but a binding or a stage
            # there is no step to short-circuit, and an empty stream is
            # nothing: `[]#$i` and `[]@$e^($)` are undefined.
            # ...and only while no `#$v` is in play: a position binding makes
            # the stream a tuple stream whose empty case is nothing at all.
            # `[].x@$e` is `[]`; `[]#$i.$` is undefined.
            keeps_empty = any(_is_navigational(st) for st in steps[1:]) and not any(
                isinstance(st, PositionBinding) for st in steps
            )
            return (
                f"cons_head({head_expr}, lambda {head_var}: {rest}, "
                f"{keep!r}, {keeps_empty!r}, {self.ctx.ctx_var})"
            )
        if isinstance(head, (ContextRef, RootRef)):
            # A leading `$`/`$$` *is* the seed, not a step over it -- so
            # `$#$pos` has one outer item and indexes 0..n-1 across it, while
            # `$.$#$pos` has a second `$` that re-parents and gives every
            # element its own index 0.
            src = self.ctx.ctx_var if isinstance(head, ContextRef) else self.ctx.root_var
            expr = f"seed({src})"
            i = 1
        elif isinstance(head, _STEP_TYPES):
            expr = f"seed({self.ctx.ctx_var})"
        else:
            # A non-path head with a step after it: the stream is seeded from
            # the *context*, and the head is a step over it -- so `@$v`
            # reverts to the context, not to the head's value.
            # `(nums)@$e.$` is the document once per element of `nums`.
            expr = f"seed({self.ctx.ctx_var})"

        return self._rest(steps, i, expr, keep)

    def _rest(self, steps: list[AstNode], i: int, expr: str, keep: bool) -> str:
        last = len(steps) - 1
        for si in range(i, last):
            expr = self._step(steps[si], expr, si == 0)
        if last >= i:
            final = self._terminal(steps[last], expr, keep)
            if final is not None:
                return final
            expr = self._step(steps[last], expr, last == 0)
        return f"collapse_tuples({expr}, {keep!r})"


def visit_tuple_path(t: Translator, n: PathExpr, ctx: GenCtx, keep: bool = False) -> str:
    return _Emitter(t, list(n.steps), ctx).emit(keep)
