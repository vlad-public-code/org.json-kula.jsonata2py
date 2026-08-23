"""Immutable per-node translation context.

Ported from org.json_kula.jsonata_jvm.translator.GenCtx.

A new instance is created whenever the current-context variable name
(ctx_var) changes (e.g. inside a predicate lambda).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .gen_state import GenState


@dataclass(frozen=True, slots=True)
class GenCtx:
    ctx_var: str  # Python variable holding the current context value
    root_var: str  # Python variable holding the document root
    state: GenState

    # Stack of parent-level Python variable names, used to resolve % (parent
    # operator). The last element is the immediate parent of ctx_var. Empty
    # if no parent context is available.
    parent_vars: tuple[str, ...] = ()

    # True if we're inside an array constructor used as a step (e.g. Email.[address]).
    in_array_constructor_step: bool = False

    # True when the array constructor path step should preserve inner arrays
    # as single items (used for the $.[arr][] pattern).
    array_constructor_preserve: bool = False

    # The Python variable name that is the cross-join base, i.e. the parent
    # from which cross-joined FieldRef steps should navigate. Set by a
    # @$var.FieldRef ContextBinding handler and propagated through
    # subsequent path steps. None when not inside a cross-join context.
    cross_join_parent: str | None = None

    # True when the expression being generated is in tail position within a
    # user-defined lambda body. When set, user function calls are emitted as
    # fn_apply_tco(...) instead of fn_apply(...), enabling the trampoline
    # loop in lambdas.fn_apply to perform TCO without growing the Python
    # call stack.
    is_tail_position: bool = False

    # When non-None, the Python variable name (e.g. "v_c") that was bound by
    # the most recent @$var ContextBinding before a GroupByExpr.
    primary_context_var: str | None = None

    # When non-None, the Python variable name for the position bound by
    # #$pos that was stashed into each sort-tuple's second element.
    tuple_pos: str | None = None

    def with_ctx(self, new_ctx: str) -> GenCtx:
        return GenCtx(
            new_ctx, self.root_var, self.state, self.parent_vars,
            self.in_array_constructor_step, self.array_constructor_preserve,
            self.cross_join_parent, False, self.primary_context_var, self.tuple_pos,
        )

    def with_ctx_and_parent(self, new_ctx: str) -> GenCtx:
        """Returns a new context with new_ctx as context and the current
        ctx_var pushed as parent."""
        return GenCtx(
            new_ctx, self.root_var, self.state, (*self.parent_vars, self.ctx_var),
            self.in_array_constructor_step, self.array_constructor_preserve,
            self.cross_join_parent, False, self.primary_context_var, self.tuple_pos,
        )

    def with_parents(self, new_parent_vars: tuple[str, ...] | list[str]) -> GenCtx:
        return GenCtx(
            self.ctx_var, self.root_var, self.state, tuple(new_parent_vars),
            self.in_array_constructor_step, self.array_constructor_preserve,
            self.cross_join_parent, self.is_tail_position, self.primary_context_var, self.tuple_pos,
        )

    def with_in_array_constructor_step(self) -> GenCtx:
        return GenCtx(
            self.ctx_var, self.root_var, self.state, self.parent_vars,
            True, self.array_constructor_preserve,
            self.cross_join_parent, False, self.primary_context_var, self.tuple_pos,
        )

    def with_array_constructor_preserve(self) -> GenCtx:
        return GenCtx(
            self.ctx_var, self.root_var, self.state, self.parent_vars,
            self.in_array_constructor_step, True,
            self.cross_join_parent, False, self.primary_context_var, self.tuple_pos,
        )

    def with_cross_join_parent(self, cjp: str | None) -> GenCtx:
        return GenCtx(
            self.ctx_var, self.root_var, self.state, self.parent_vars,
            self.in_array_constructor_step, self.array_constructor_preserve,
            cjp, self.is_tail_position, self.primary_context_var, self.tuple_pos,
        )

    def with_tail_position(self, tp: bool) -> GenCtx:
        return GenCtx(
            self.ctx_var, self.root_var, self.state, self.parent_vars,
            self.in_array_constructor_step, self.array_constructor_preserve,
            self.cross_join_parent, tp, self.primary_context_var, self.tuple_pos,
        )

    def with_primary_context_var(self, pcv: str | None) -> GenCtx:
        return GenCtx(
            self.ctx_var, self.root_var, self.state, self.parent_vars,
            self.in_array_constructor_step, self.array_constructor_preserve,
            self.cross_join_parent, self.is_tail_position, pcv, self.tuple_pos,
        )

    def with_tuple_pos(self, tp: str | None) -> GenCtx:
        return GenCtx(
            self.ctx_var, self.root_var, self.state, self.parent_vars,
            self.in_array_constructor_step, self.array_constructor_preserve,
            self.cross_join_parent, self.is_tail_position, self.primary_context_var, tp,
        )
