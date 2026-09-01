"""Mutable state shared across all GenCtx instances in one translation.

Ported from org.json_kula.jsonata_jvm.translator.GenState.

Also home to the callback-hoisting machinery (`emit_callback` /
`hoistable_callback`), which has no Java counterpart: it is a port of
jsonata2js's round-five optimisation (`translator.js#hoistableClosure`,
`gen-ctx.js#hoistClosure`). It lives here rather than in translator.py
because path_codegen.py needs it too and cannot import translator.py at
module level (translator.py imports path_codegen.py).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING

from ..parser.ast_nodes import (
    ArrayConstructor,
    ArraySubscript,
    AstNode,
    BinaryOp,
    BooleanLiteral,
    CoalesceExpr,
    ConditionalExpr,
    ContextRef,
    DescendantStep,
    ElvisExpr,
    FieldRef,
    ForceArray,
    FunctionCall,
    NullLiteral,
    NumberLiteral,
    ObjectConstructor,
    Parenthesized,
    PathExpr,
    PredicateExpr,
    RangeExpr,
    RegexLiteral,
    StringLiteral,
    UnaryMinus,
    WildcardStep,
)

if TYPE_CHECKING:
    from .gen_ctx import GenCtx


class GenState:
    def __init__(self) -> None:
        self.counter = 0
        # Memoises scope_analyzer.contains_parent_step by id(node) for the
        # lifetime of this translation -- the translator calls it repeatedly
        # on overlapping subtrees from several call sites. Never module-
        # level: id() is reused once a node is freed.
        self._contains_parent_step_cache: dict[int, bool] = {}
        # Buffer of complete top-level `def ...` blocks, appended as they're
        # generated (mirrors Java's helperMethods StringBuilder).
        self.helper_defs: list[str] = []
        # Local declaration lines emitted before the body expression inside
        # _evaluate (e.g. counter-list declarations for global #$pos counters).
        self.local_declarations: list[str] = []

        # Literal values hoisted to module-level constants, keyed by the
        # Python expression that builds them. Per D8.4/6.4: only containers
        # (dict/list literals) are worth hoisting in Python -- scalars are
        # already interned in the code object -- but object-constructor key
        # tuples are still hoisted here too.
        self._constants: dict[str, str] = {}
        self._key_arrays: dict[str, str] = {}

        # Per-element callbacks hoisted to module-level constants, keyed by
        # the `lambda ...: ...` source that builds them, so two textually
        # identical predicates share one constant. See emit_callback.
        self._callbacks: dict[str, str] = {}

        # Nesting depth of canonical callback parameters (_p0, _p1, ...)
        # inside the hoist unit currently being generated; 0 when not inside
        # one. Canonical names are what let identical predicates dedupe:
        # minted `_el17`-style names would differ per occurrence.
        self.canon_param_depth = 0

        # Stack of locally-bound variable name sets, one entry per active
        # scope (block or lambda body). isLocal() decides whether a
        # VariableRef should resolve to a Python local variable or to a
        # runtime binding lookup.
        self.scope_stack: list[set[str]] = []

        # Variables that use an array-holder pattern for recursive
        # self-reference. When name is in this set, visit_variable_ref emits
        # v_nameRef[0] instead of v_name.
        self.holder_vars: set[str] = set()

        # Per-scope alias maps: when a multi-param lambda uses id-suffixed
        # Python names to avoid shadowing, the canonical JSONata name maps
        # to its Python alias. Mirrors scope_stack -- pushed/popped together.
        self._alias_stack: list[dict[str, str]] = []

        # Set to a variable name while generating a PartialApplication body.
        self.partial_ph_var: str | None = None
        self.partial_ph_need_idx = False
        self.partial_ph_idx = 0

    def next_id(self) -> int:
        i = self.counter
        self.counter += 1
        return i

    def contains_parent_step(self, node: AstNode | None) -> bool:
        """Memoised scope_analyzer.contains_parent_step -- prefer this over
        calling the module function directly from translator code."""
        from . import scope_analyzer

        return scope_analyzer.contains_parent_step(node, self._contains_parent_step_cache)

    # -------------------------------------------------------------------------
    # Constant / key-array / callback hoisting
    # -------------------------------------------------------------------------

    def constant(self, python_expression: str) -> str:
        """Returns the module-level name holding python_expression, creating
        it if needed."""
        name = self._constants.get(python_expression)
        if name is None:
            name = f"_const{len(self._constants)}"
            self._constants[python_expression] = name
        return name

    def key_array(self, python_initialiser: str) -> str:
        """Returns the module-level name holding the given tuple/list
        initialiser."""
        name = self._key_arrays.get(python_initialiser)
        if name is None:
            name = f"_keys{len(self._key_arrays)}"
            self._key_arrays[python_initialiser] = name
        return name

    def hoisted_callback(self, python_lambda: str) -> str:
        """Returns the module-level name holding python_lambda, creating it
        if needed. Deduped on the source text, so identical predicates share
        one constant (see emit_callback for how the names are canonicalised
        to make that happen)."""
        name = self._callbacks.get(python_lambda)
        if name is None:
            name = f"_fn{len(self._callbacks)}"
            self._callbacks[python_lambda] = name
        return name

    def constant_declarations(self) -> str:
        """Emits the module-level assignment lines for every hoisted literal,
        key array and callback."""
        if not self._constants and not self._key_arrays and not self._callbacks:
            return ""
        lines = []
        for expression, name in self._constants.items():
            lines.append(f"{name} = {expression}")
        for initialiser, name in self._key_arrays.items():
            lines.append(f"{name} = {initialiser}")
        for python_lambda, name in self._callbacks.items():
            lines.append(f"{name} = {python_lambda}")
        return "\n".join(lines) + "\n"

    # -------------------------------------------------------------------------
    # Helper `def` generation (Python lambdas can't contain statements)
    # -------------------------------------------------------------------------

    def new_helper_def(self, name_prefix: str, params: list[str], body_lines: list[str]) -> str:
        """Appends a top-level `def name(params): body_lines` to
        helper_defs and returns the function name. body_lines must already
        end with a return statement (or raise)."""
        name = f"_{name_prefix}{self.next_id()}"
        header = f"def {name}({', '.join(params)}):"
        indented = "\n".join(f"    {line}" for line in body_lines)
        self.helper_defs.append(f"{header}\n{indented}\n")
        return name

    # -------------------------------------------------------------------------
    # Scope management
    # -------------------------------------------------------------------------

    def push_scope(self) -> None:
        self.scope_stack.append(set())
        self._alias_stack.append({})

    def pop_scope(self) -> None:
        if self.scope_stack:
            self.scope_stack.pop()
            self._alias_stack.pop()

    def add_local_var(self, name: str) -> None:
        if self.scope_stack:
            self.scope_stack[-1].add(name)

    def add_local_var_with_alias(self, name: str, python_name: str) -> None:
        if self.scope_stack:
            self.scope_stack[-1].add(name)
            self._alias_stack[-1][name] = python_name

    def is_local(self, name: str) -> bool:
        return any(name in scope for scope in self.scope_stack)

    def get_alias(self, name: str) -> str | None:
        """Returns the Python alias for name in the innermost scope that
        defines name, or None if no alias exists in that scope. Searches
        scope_stack/alias_stack from innermost (end of list) to outermost,
        stopping at the defining scope."""
        for scope, aliases in zip(reversed(self.scope_stack), reversed(self._alias_stack), strict=True):
            if name in scope:
                return aliases.get(name)
        return None


# =============================================================================
# Callback hoisting
# =============================================================================
#
# Ported from jsonata2js `translator.js#hoistableClosure` /
# `#genElementCallback` (round-five optimisation, its
# docs/design/performance.md items 23-24). Every per-element callback whose
# body provably cannot reach anything in the evaluator's own scope is moved
# to a module-level constant, so it is built once per *compiled expression*
# instead of once per `evaluate()` call.
#
# The evaluator's scope is exactly: `_root` (the document root parameter),
# `_ctx` (the outer context parameter), the `v_name` / `_ref_name` locals a
# block introduces, and any enclosing per-element closure's `%` parent /
# `@$`,`#$` binding variables. Everything else a predicate normally contains
# -- literals, `$`-rooted navigation, comparisons, arithmetic, static
# built-in calls, hoisted key arrays, module-level runtime functions pulled
# in by `from jsonata2py.runtime import *` -- refers only to the callback's
# own parameters or to module-level names.

# Built-in names whose `Translator._dispatch_builtin` arm emits nothing but
# `fn_*(...)` over its arguments and `ctx.ctx_var` (which, inside a callback,
# *is* the callback's own parameter).
#
# Deliberately excluded, and why:
#   * sort/map/filter/each/reduce/single/sift -- routed through
#     call_codegen, which generates a named helper `def` called with
#     `ctx.root_var` threaded in as an argument.
#   * every name not listed at all -- `_dispatch_builtin` falls through to
#     `call_codegen.gen_user_function_call`, which also threads
#     `ctx.root_var`.
# A name added to `_dispatch_builtin` but not here simply doesn't hoist.
_STATIC_BUILTINS = frozenset(
    {
        "abs", "append", "assert", "average", "base64decode", "base64encode",
        "boolean", "ceil", "clone", "contains", "count", "decodeUrl",
        "decodeUrlComponent", "distinct", "encodeUrl", "encodeUrlComponent",
        "error", "eval", "exists", "flatten", "floor", "formatBase",
        "formatInteger", "formatNumber", "fromMillis", "join", "keys",
        "length", "lookup", "lowercase", "match", "max", "merge", "millis",
        "min", "not", "now", "pad", "parseInteger", "power", "random",
        "replace", "reverse", "round", "shuffle", "split", "spread", "sqrt",
        "string", "substring", "substringAfter", "substringBefore", "sum",
        "trim", "type", "uppercase", "values", "zip",
    }
)


def _h_any(node: AstNode, state: GenState) -> bool:
    return True


def _h_path(node: PathExpr, state: GenState) -> bool:
    return all(_hoistable(step, state) for step in node.steps)


def _h_source_predicate(node: PredicateExpr, state: GenState) -> bool:
    return _hoistable(node.source, state) and _hoistable(node.predicate, state)


def _h_source_index(node: ArraySubscript, state: GenState) -> bool:
    return _hoistable(node.source, state) and _hoistable(node.index, state)


def _h_left_right(node: BinaryOp | ElvisExpr | CoalesceExpr, state: GenState) -> bool:
    return _hoistable(node.left, state) and _hoistable(node.right, state)


def _h_operand(node: UnaryMinus, state: GenState) -> bool:
    return _hoistable(node.operand, state)


def _h_range(node: RangeExpr, state: GenState) -> bool:
    return _hoistable(node.from_, state) and _hoistable(node.to, state)


def _h_inner(node: Parenthesized, state: GenState) -> bool:
    return _hoistable(node.inner, state)


def _h_source(node: ForceArray, state: GenState) -> bool:
    return _hoistable(node.source, state)


def _h_conditional(node: ConditionalExpr, state: GenState) -> bool:
    return (
        _hoistable(node.condition, state)
        and _hoistable(node.then, state)
        and (node.otherwise is None or _hoistable(node.otherwise, state))
    )


def _h_elements(node: ArrayConstructor, state: GenState) -> bool:
    return all(_hoistable(element, state) for element in node.elements)


def _h_pairs(node: ObjectConstructor, state: GenState) -> bool:
    return all(_hoistable(pair.key, state) and _hoistable(pair.value, state) for pair in node.pairs)


def _h_call(node: FunctionCall, state: GenState) -> bool:
    # A lexically bound name shadows the built-in and compiles to a user
    # function call, which reaches the evaluator's scope.
    if node.name not in _STATIC_BUILTINS or state.is_local(node.name):
        return False
    return all(_hoistable(arg, state) for arg in node.args)


# CLOSED whitelist: an AST class absent from this table means "not
# hoistable". Absent on purpose, each because its codegen reaches the
# evaluator's scope: RootRef (`_root`), VariableRef (a `v_name` local, a
# `_ref_name` holder box, or a runtime binding lookup), ParentStep and
# ContextBinding/PositionBinding (`%` / `@$` / `#$` per-element state),
# Lambda/LambdaCall/PartialApplication/PartialPlaceholder and
# Block/VariableBinding (named helper `def`s called with `_root` and the
# captured locals threaded in), SortExpr/GroupByExpr/ChainExpr/
# TransformExpr/TransformLambda (same, or nested per-element closures over
# outer state).
_HOISTABLE: dict[type, Callable[..., bool]] = {
    StringLiteral: _h_any,
    NumberLiteral: _h_any,
    BooleanLiteral: _h_any,
    NullLiteral: _h_any,
    RegexLiteral: _h_any,  # regex_value() is a module-level runtime function
    ContextRef: _h_any,  # the callback's own parameter
    FieldRef: _h_any,
    WildcardStep: _h_any,
    DescendantStep: _h_any,
    PathExpr: _h_path,
    PredicateExpr: _h_source_predicate,
    ArraySubscript: _h_source_index,
    BinaryOp: _h_left_right,
    ElvisExpr: _h_left_right,
    CoalesceExpr: _h_left_right,
    UnaryMinus: _h_operand,
    RangeExpr: _h_range,
    Parenthesized: _h_inner,
    ForceArray: _h_source,
    ConditionalExpr: _h_conditional,
    ArrayConstructor: _h_elements,
    ObjectConstructor: _h_pairs,
    FunctionCall: _h_call,
}


def _hoistable(node: AstNode | None, state: GenState) -> bool:
    if node is None:
        return True
    rule = _HOISTABLE.get(node.__class__)
    return rule is not None and rule(node, state)


def hoistable_callback(node: AstNode, ctx: GenCtx) -> bool:
    """True when the callback compiled from `node` (with the callback's own
    parameter as the context) can be moved to module scope."""
    # Any of these means an enclosing per-element closure has state the
    # callback body may resolve against; the identifiers holding it are
    # locals of that closure, not module-level names.
    if ctx.parent_vars or ctx.cross_join_parent is not None:
        return False
    if ctx.tuple_pos is not None or ctx.primary_context_var is not None:
        return False
    return _hoistable(node, ctx.state)


# Evaluator-scope identifiers, matched as whole tokens: `v_` / `_ref_`
# (naming.pyvar / naming.pyvar_ref) cover block locals, while `_root` and
# `_ctx` are the generated functions' own parameters.
#
# Whole-token matching is load-bearing, not tidiness: a plain substring test
# also fires on runtime helpers whose *names* embed a fragment -- notably
# `call_builtin_ctx` -- silently costing every call site that mentions one
# its hoist. A string literal that happens to contain a fragment still
# costs a missed hoist, which is a size trade, never a correctness one.
_EVALUATOR_SCOPE_NAMES = re.compile(r"\b(?:_root|_ctx|v_\w+|_ref_\w+)\b")


def _leaks_evaluator_scope(code: str) -> bool:
    """Belt to the whitelist's braces: rejects a body that mentions any
    evaluator-scope identifier."""
    return _EVALUATOR_SCOPE_NAMES.search(code) is not None


def emit_callback(node: AstNode, ctx: GenCtx, prefix: str, build: Callable[[str], str]) -> str:
    """Emits a per-element callback over `node` as either a module-level
    constant's name or an inline `lambda <param>: <body>`, whichever the
    hoistability whitelist allows. `build(param)` compiles the body with
    `param` bound as the context variable.

    A hoisted callback gets canonical parameter names (`_p0` outermost,
    `_p1`, ... for any callback nested inside it that stays inline) so two
    textually identical predicates collapse to one constant instead of
    differing only by minted identifiers.
    """
    state = ctx.state
    depth = state.canon_param_depth
    hoist = hoistable_callback(node, ctx)
    if not hoist and depth == 0:
        var = f"{prefix}{state.next_id()}"
        return f"lambda {var}: {build(var)}"
    # Inside a hoist unit (depth > 0) the canonical names continue rather
    # than restarting, so an inline nested callback can never shadow the
    # parameter of the unit it lives in.
    var = f"_p{depth}"
    state.canon_param_depth = depth + 1
    try:
        body = build(var)
    finally:
        state.canon_param_depth = depth
    code = f"lambda {var}: {body}"
    if hoist and not _leaks_evaluator_scope(code):
        return state.hoisted_callback(code)
    return code
