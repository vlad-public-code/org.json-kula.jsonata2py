"""Single-pass, bottom-up AST optimizer for JSONata expressions.

Ported from org.json_kula.jsonata_jvm.optimizer.Optimizer.

Each optimization pass rewrites children first (post-order), then
applies local rewrite rules to the resulting node. A single call to
optimize(node) applies one full pass.

Rewrites applied:
  - Constant folding: evaluates operators whose operands are all
    compile-time constants (number, string, boolean, null): arithmetic
    (+ - * / %), string concatenation (&), comparisons (= != < <= > >=),
    and boolean logic (and, or).
  - Arithmetic identity / absorption: x+0, x*1, x*0, x/1, x-0, and
    their symmetric variants.
  - String identity: x & "" and "" & x -> x.
  - Boolean short-circuit identities: x and false -> false,
    x or true -> true, x and true -> x, x or false -> x.
  - Conditional folding: when the condition is a literal
    true/false/null.
  - Unary-minus elimination: -NumberLiteral(v) -> NumberLiteral(-v);
    double negation -(-x) -> x.
  - Block unwrapping: a Block with exactly one expression is replaced
    by that expression.
  - PathExpr flattening: nested PathExpr nodes are merged into a single
    flat list of steps.

Unsafe operations are left intact: division or modulo by the literal
zero is not folded so that the runtime can report a meaningful error.

The optimizer never mutates the original node; it returns a
(possibly identical, by reference) optimized copy. Since AstNode
dataclasses are frozen and comparable, `is` identity checks are used
as a fast path to avoid unnecessary allocation, exactly mirroring the
Java `==` reference-identity checks.
"""

from __future__ import annotations

import math

from ..parser.ast_nodes import (
    ArrayConstructor,
    ArraySubscript,
    AstNode,
    BinaryOp,
    Block,
    BooleanLiteral,
    ChainExpr,
    CoalesceExpr,
    ConditionalExpr,
    ContextBinding,
    ContextRef,
    DescendantStep,
    ElvisExpr,
    FieldRef,
    ForceArray,
    FunctionCall,
    GroupByExpr,
    KeyValuePair,
    Lambda,
    LambdaCall,
    NullLiteral,
    NumberLiteral,
    ObjectConstructor,
    Parenthesized,
    ParentStep,
    PartialApplication,
    PartialPlaceholder,
    PathExpr,
    PositionBinding,
    PredicateExpr,
    RangeExpr,
    RegexLiteral,
    RootRef,
    SortExpr,
    SortKey,
    StringLiteral,
    TransformExpr,
    TransformLambda,
    UnaryMinus,
    VariableBinding,
    VariableRef,
    Visitor,
    WildcardStep,
    accept,
)


def optimize(node: AstNode) -> AstNode:
    """Returns an optimized copy of node. The original node is never
    mutated."""
    return _VISITOR.rewrite(node)


class _RewriteVisitor(Visitor[AstNode, None]):
    def rewrite(self, node: AstNode) -> AstNode:
        return accept(node, self, None)

    # ---- Terminals -- returned as-is ----

    def visit_string_literal(self, node: StringLiteral, ctx: None) -> AstNode:
        return node

    def visit_number_literal(self, node: NumberLiteral, ctx: None) -> AstNode:
        return node

    def visit_boolean_literal(self, node: BooleanLiteral, ctx: None) -> AstNode:
        return node

    def visit_null_literal(self, node: NullLiteral, ctx: None) -> AstNode:
        return node

    def visit_regex_literal(self, node: RegexLiteral, ctx: None) -> AstNode:
        return node

    def visit_context_ref(self, node: ContextRef, ctx: None) -> AstNode:
        return node

    def visit_root_ref(self, node: RootRef, ctx: None) -> AstNode:
        return node

    def visit_variable_ref(self, node: VariableRef, ctx: None) -> AstNode:
        return node

    def visit_field_ref(self, node: FieldRef, ctx: None) -> AstNode:
        return node

    def visit_wildcard_step(self, node: WildcardStep, ctx: None) -> AstNode:
        return node

    def visit_descendant_step(self, node: DescendantStep, ctx: None) -> AstNode:
        return node

    def visit_parent_step(self, node: ParentStep, ctx: None) -> AstNode:
        return node

    def visit_position_binding(self, node: PositionBinding, ctx: None) -> AstNode:
        return node

    def visit_context_binding(self, node: ContextBinding, ctx: None) -> AstNode:
        return node

    def visit_partial_placeholder(self, node: PartialPlaceholder, ctx: None) -> AstNode:
        return node

    # ---- Unary minus ----

    def visit_unary_minus(self, node: UnaryMinus, ctx: None) -> AstNode:
        operand = self.rewrite(node.operand)
        # -NumberLiteral(v) -> NumberLiteral(-v)
        if isinstance(operand, NumberLiteral):
            return NumberLiteral(-operand.value)
        # -(-x) -> x
        if isinstance(operand, UnaryMinus):
            return operand.operand
        return node if operand is node.operand else UnaryMinus(operand)

    # ---- Binary operations ----

    def visit_binary_op(self, node: BinaryOp, ctx: None) -> AstNode:
        left = self.rewrite(node.left)
        right = self.rewrite(node.right)

        folded = _try_fold(node.op, left, right)
        if folded is not None:
            return folded

        return node if left is node.left and right is node.right else BinaryOp(node.op, left, right)

    # ---- Conditional ----

    def visit_conditional_expr(self, node: ConditionalExpr, ctx: None) -> AstNode:
        condition = self.rewrite(node.condition)
        then = self.rewrite(node.then)
        otherwise = self.rewrite(node.otherwise) if node.otherwise is not None else None

        # true ? a : b -> a
        if isinstance(condition, BooleanLiteral) and condition.value:
            return then
        # false ? a : b -> b   (or null if no else-branch)
        if isinstance(condition, BooleanLiteral) and not condition.value:
            return otherwise if otherwise is not None else NullLiteral()
        # null ? a : b -> b
        if isinstance(condition, NullLiteral):
            return otherwise if otherwise is not None else NullLiteral()

        if condition is node.condition and then is node.then and otherwise is node.otherwise:
            return node
        return ConditionalExpr(condition, then, otherwise)

    # ---- Block ----

    def visit_block(self, node: Block, ctx: None) -> AstNode:
        exprs = self._rewrite_list(node.expressions)
        # Single-expression block: unwrap -- UNLESS it is a self-referential
        # VariableBinding whose lambda body references the binding name. In
        # that case the Block must be preserved so BlockCodeGen can apply the
        # holder-array pattern and make the name available in scope.
        if len(exprs) == 1:
            only = exprs[0]
            is_self_ref = (
                isinstance(only, VariableBinding)
                and isinstance(only.value, Lambda)
                and _lambda_body_references_name(only.value.body, only.name)
            )
            if not is_self_ref:
                return only
        return node if exprs == node.expressions else Block(exprs)

    # ---- Path expression ----

    def visit_path_expr(self, node: PathExpr, ctx: None) -> AstNode:
        # Rewrite each step, then flatten nested PathExprs.
        # NOTE: Parenthesized nodes that appear as path steps MUST be preserved
        # (not stripped) so the translator can detect cross-join patterns such
        # as @$l.(library.books) where the parenthesized sub-expression is
        # evaluated from the document root rather than the current context
        # element.
        flat: list[AstNode] = []
        prev_was_context_binding = False
        for step in node.steps:
            if prev_was_context_binding and isinstance(step, Parenthesized):
                # Preserve the Parenthesized wrapper so the translator can
                # detect the cross-join pattern: rewrite only the inner
                # expression.
                inner_rewritten = self.rewrite(step.inner)
                flat.append(step if inner_rewritten is step.inner else Parenthesized(inner_rewritten))
                prev_was_context_binding = False
                continue
            rewritten = self.rewrite(step)
            if isinstance(rewritten, PathExpr) and not _path_has_wildcard(rewritten):
                flat.extend(rewritten.steps)
            else:
                flat.append(rewritten)
            prev_was_context_binding = isinstance(rewritten, ContextBinding)
        return node if flat == node.steps else PathExpr(flat)

    # ---- Predicate / subscript ----

    def visit_predicate_expr(self, node: PredicateExpr, ctx: None) -> AstNode:
        source = self.rewrite(node.source)
        predicate = self.rewrite(node.predicate)
        return node if source is node.source and predicate is node.predicate else PredicateExpr(source, predicate)

    def visit_array_subscript(self, node: ArraySubscript, ctx: None) -> AstNode:
        source = self.rewrite(node.source)
        index = self.rewrite(node.index)
        return node if source is node.source and index is node.index else ArraySubscript(source, index)

    # ---- Constructors ----

    def visit_array_constructor(self, node: ArrayConstructor, ctx: None) -> AstNode:
        elements = self._rewrite_list(node.elements)
        return node if elements == node.elements else ArrayConstructor(elements)

    def visit_object_constructor(self, node: ObjectConstructor, ctx: None) -> AstNode:
        pairs = self._rewrite_pairs(node.pairs)
        return node if pairs == node.pairs else ObjectConstructor(pairs)

    # ---- Functions / lambdas / binding ----

    def visit_function_call(self, node: FunctionCall, ctx: None) -> AstNode:
        args = self._rewrite_list(node.args)
        return node if args == node.args else FunctionCall(node.name, args)

    def visit_lambda(self, node: Lambda, ctx: None) -> AstNode:
        body = self.rewrite(node.body)
        return node if body is node.body else Lambda(node.params, body)

    def visit_variable_binding(self, node: VariableBinding, ctx: None) -> AstNode:
        value = self.rewrite(node.value)
        return node if value is node.value else VariableBinding(node.name, value)

    # ---- Range, sort, group-by, chain, transform ----

    def visit_range_expr(self, node: RangeExpr, ctx: None) -> AstNode:
        from_ = self.rewrite(node.from_)
        to = self.rewrite(node.to)
        return node if from_ is node.from_ and to is node.to else RangeExpr(from_, to)

    def visit_sort_expr(self, node: SortExpr, ctx: None) -> AstNode:
        source = self.rewrite(node.source)
        keys = [SortKey(self.rewrite(k.key), k.descending) for k in node.keys]
        return node if source is node.source and keys == node.keys else SortExpr(source, keys)

    def visit_group_by_expr(self, node: GroupByExpr, ctx: None) -> AstNode:
        source = self.rewrite(node.source)
        pairs = self._rewrite_pairs(node.pairs)
        # Rule D: if source contains @$var or #$var bindings, unfold to a
        # PathExpr so the binding steps become path steps and binding
        # variables remain in scope when the GroupBy key/value expressions
        # are compiled.
        if _contains_binding(source):
            steps: list[AstNode] = []
            _unfold_to_path_steps(source, steps)
            steps.append(GroupByExpr(ContextRef(), pairs))
            return PathExpr(steps)
        return node if source is node.source and pairs == node.pairs else GroupByExpr(source, pairs)

    def visit_chain_expr(self, node: ChainExpr, ctx: None) -> AstNode:
        steps = self._rewrite_list(node.steps)
        return node if steps == node.steps else ChainExpr(steps)

    def visit_parenthesized(self, node: Parenthesized, ctx: None) -> AstNode:
        # The Parenthesized wrapper only exists to suppress path-step
        # subscript folding during parsing. Once parsing is done the AST
        # structure itself encodes the distinction: a folded subscript lives
        # *inside* the PathExpr steps, while a parenthesised subscript wraps
        # the PathExpr with a plain ArraySubscript. The wrapper can therefore
        # be stripped here so that constant-folding and other rewrites see
        # through it normally.
        # Exception: preserve the wrapper when inner is a VariableBinding (or
        # a Block containing bindings) so that parenthesised assignments do
        # not inadvertently reassign outer-scope variables of the same name.
        inner = self.rewrite(node.inner)
        if isinstance(inner, VariableBinding):
            return Parenthesized(inner)
        return inner

    def visit_force_array(self, node: ForceArray, ctx: None) -> AstNode:
        source = self.rewrite(node.source)
        return node if source is node.source else ForceArray(source)

    def visit_transform_expr(self, node: TransformExpr, ctx: None) -> AstNode:
        source = self.rewrite(node.source)
        pattern = self.rewrite(node.pattern)
        update = self.rewrite(node.update)
        delete = self.rewrite(node.delete) if node.delete is not None else None
        if source is node.source and pattern is node.pattern and update is node.update and delete is node.delete:
            return node
        return TransformExpr(source, pattern, update, delete)

    def visit_transform_lambda(self, node: TransformLambda, ctx: None) -> AstNode:
        pattern = self.rewrite(node.pattern)
        update = self.rewrite(node.update)
        delete = self.rewrite(node.delete) if node.delete is not None else None
        if pattern is node.pattern and update is node.update and delete is node.delete:
            return node
        return TransformLambda(pattern, update, delete)

    def visit_elvis_expr(self, node: ElvisExpr, ctx: None) -> AstNode:
        left = self.rewrite(node.left)
        right = self.rewrite(node.right)
        return node if left is node.left and right is node.right else ElvisExpr(left, right)

    def visit_coalesce_expr(self, node: CoalesceExpr, ctx: None) -> AstNode:
        left = self.rewrite(node.left)
        right = self.rewrite(node.right)
        return node if left is node.left and right is node.right else CoalesceExpr(left, right)

    def visit_partial_application(self, node: PartialApplication, ctx: None) -> AstNode:
        args = self._rewrite_list(node.args)
        return node if args == node.args else PartialApplication(node.name, args)

    def visit_lambda_call(self, node: LambdaCall, ctx: None) -> AstNode:
        lambda_node = self.rewrite(node.lambda_)
        args = self._rewrite_list(node.args)
        if lambda_node is node.lambda_ and args == node.args:
            return node
        assert isinstance(lambda_node, Lambda)
        return LambdaCall(lambda_node, args)

    # =====================================================================
    # Helpers
    # =====================================================================

    def _rewrite_list(self, nodes: list[AstNode]) -> list[AstNode]:
        result: list[AstNode] = []
        changed = False
        for n in nodes:
            r = self.rewrite(n)
            result.append(r)
            if r is not n:
                changed = True
        return result if changed else nodes

    def _rewrite_pairs(self, pairs: list[KeyValuePair]) -> list[KeyValuePair]:
        result: list[KeyValuePair] = []
        changed = False
        for p in pairs:
            k = self.rewrite(p.key)
            v = self.rewrite(p.value)
            rp = p if (k is p.key and v is p.value) else KeyValuePair(k, v)
            result.append(rp)
            if rp is not p:
                changed = True
        return result if changed else pairs


_VISITOR = _RewriteVisitor()


# =============================================================================
# Constant-folding logic
# =============================================================================


def _try_fold(op: str, left: AstNode, right: AstNode) -> AstNode | None:
    """Attempts to fold a binary operation whose operands are already
    (partially) optimized. Returns the folded node, or None if no rule
    matched."""
    # --- Number x Number ---
    if isinstance(left, NumberLiteral) and isinstance(right, NumberLiteral):
        return _fold_num_num(op, left.value, right.value)
    # --- String x String ---
    if isinstance(left, StringLiteral) and isinstance(right, StringLiteral):
        return _fold_str_str(op, left.value, right.value)
    # --- Boolean x Boolean ---
    if isinstance(left, BooleanLiteral) and isinstance(right, BooleanLiteral):
        return _fold_bool_bool(op, left.value, right.value)
    # --- Boolean short-circuit identities (one side is a literal) ---
    bool_fold = _try_fold_bool_identity(op, left, right)
    if bool_fold is not None:
        return bool_fold

    # NOTE: there are deliberately no arithmetic ("x + 0", "x * 1", "x * 0")
    # or concatenation ("x & \"\"") identity rules for a *non-literal* operand.
    # They are only sound when the other side is provably a number (resp. a
    # string), which the AST cannot prove: "s * 0" must raise T2001 rather than
    # fold to 0, "nothere * 0" must stay undefined, and "n & \"\"" must produce
    # the string "5", not the number 5. When both sides are literals the
    # _fold_num_num / _fold_str_str paths above already handle them.
    return None


def _fold_num_num(op: str, left: float, right: float) -> AstNode | None:
    # Guard: do not fold division/modulo by zero -- leave for runtime
    if op in ("/", "%") and right == 0:
        return None
    # Guard: do not fold arithmetic whose result would be non-finite -- the
    # reference reports "Number out of range" at evaluation time, and a
    # NumberLiteral(inf) is not emittable as Python source anyway. This is
    # needed for proper error reporting in cases like 1/(10e300 * 10e100)
    # and for 1e308 + 1e308, which used to fold to inf and crash codegen.
    if op in ("+", "-", "*"):
        result = left + right if op == "+" else left - right if op == "-" else left * right
        if not math.isfinite(result):
            return None
        return NumberLiteral(result)
    if op == "/":
        return NumberLiteral(left / right) if right != 0 else None
    if op == "%":
        return NumberLiteral(_java_fmod(left, right)) if right != 0 else None
    if op == "=":
        return BooleanLiteral(left == right)
    if op == "!=":
        return BooleanLiteral(left != right)
    if op == "<":
        return BooleanLiteral(left < right)
    if op == "<=":
        return BooleanLiteral(left <= right)
    if op == ">":
        return BooleanLiteral(left > right)
    if op == ">=":
        return BooleanLiteral(left >= right)
    return None


def _java_fmod(left: float, right: float) -> float:
    """Java's % on doubles is IEEE remainder truncated-toward-zero (same
    sign as the dividend), which matches Python's math.fmod, not Python's %
    operator (which follows the divisor's sign)."""
    return math.fmod(left, right)


def _fold_str_str(op: str, left: str, right: str) -> AstNode | None:
    if op == "&":
        return StringLiteral(left + right)
    if op == "=":
        return BooleanLiteral(left == right)
    if op == "!=":
        return BooleanLiteral(left != right)
    if op == "<":
        return BooleanLiteral(left < right)
    if op == "<=":
        return BooleanLiteral(left <= right)
    if op == ">":
        return BooleanLiteral(left > right)
    if op == ">=":
        return BooleanLiteral(left >= right)
    return None


def _fold_bool_bool(op: str, left: bool, right: bool) -> AstNode | None:
    if op == "and":
        return BooleanLiteral(left and right)
    if op == "or":
        return BooleanLiteral(left or right)
    if op == "=":
        return BooleanLiteral(left == right)
    if op == "!=":
        return BooleanLiteral(left != right)
    return None


def _try_fold_bool_identity(op: str, left: AstNode, right: AstNode) -> AstNode | None:
    """Boolean identity/absorption rules where only one side is a literal."""
    if op == "and":
        # Absorption: one false side -> always false
        if _is_false(left) or _is_false(right):
            return BooleanLiteral(False)
        # Identity: true and x -> x, but ONLY when x is already a boolean
        # literal. For non-boolean x the runtime wraps the result in
        # bool(...); returning x raw would drop that wrapping and change the
        # result type.
        if _is_true(left) and isinstance(right, BooleanLiteral):
            return right
        if _is_true(right) and isinstance(left, BooleanLiteral):
            return left
    if op == "or":
        # Absorption: one true side -> always true
        if _is_true(left) or _is_true(right):
            return BooleanLiteral(True)
        # Identity: false or x -> x, but ONLY when x is already a boolean literal.
        if _is_false(left) and isinstance(right, BooleanLiteral):
            return right
        if _is_false(right) and isinstance(left, BooleanLiteral):
            return left
    return None


# ---- Helpers ----


def _is_true(n: AstNode) -> bool:
    return isinstance(n, BooleanLiteral) and n.value


def _is_false(n: AstNode) -> bool:
    return isinstance(n, BooleanLiteral) and not n.value


def _contains_binding(node: AstNode) -> bool:
    if isinstance(node, (ContextBinding, PositionBinding)):
        return True
    if isinstance(node, PathExpr):
        return any(_contains_binding(s) for s in node.steps)
    if isinstance(node, PredicateExpr):
        return _contains_binding(node.source)
    if isinstance(node, SortExpr):
        return _contains_binding(node.source)
    return False


def _unfold_to_path_steps(source: AstNode, steps: list[AstNode]) -> None:
    if isinstance(source, PathExpr):
        steps.extend(source.steps)
    elif isinstance(source, PredicateExpr):
        _unfold_to_path_steps(source.source, steps)
        steps.append(PredicateExpr(ContextRef(), source.predicate))
    else:
        steps.append(source)


def _lambda_body_references_name(node: AstNode, name: str) -> bool:
    """Returns True if node contains a reference to name as a free variable
    (FunctionCall or VariableRef not shadowed by a lambda parameter or block
    binding). Used to detect self-referential lambdas."""
    return _lambda_body_ref(node, name, frozenset())


def _lambda_body_ref(node: AstNode | None, name: str, bound: frozenset[str]) -> bool:
    if node is None:
        return False
    if isinstance(node, VariableRef):
        return node.name not in bound and node.name == name
    if isinstance(node, FunctionCall):
        if node.name not in bound and node.name == name:
            return True
        return any(_lambda_body_ref(a, name, bound) for a in node.args)
    if isinstance(node, Lambda):
        inner = bound | set(node.params)
        return _lambda_body_ref(node.body, name, inner)
    if isinstance(node, Block):
        return any(_lambda_body_ref(e, name, bound) for e in node.expressions)
    if isinstance(node, VariableBinding):
        return _lambda_body_ref(node.value, name, bound)
    if isinstance(node, BinaryOp):
        return _lambda_body_ref(node.left, name, bound) or _lambda_body_ref(node.right, name, bound)
    if isinstance(node, UnaryMinus):
        return _lambda_body_ref(node.operand, name, bound)
    if isinstance(node, ConditionalExpr):
        return (
            _lambda_body_ref(node.condition, name, bound)
            or _lambda_body_ref(node.then, name, bound)
            or (node.otherwise is not None and _lambda_body_ref(node.otherwise, name, bound))
        )
    if isinstance(node, PathExpr):
        return any(_lambda_body_ref(s, name, bound) for s in node.steps)
    if isinstance(node, ArrayConstructor):
        return any(_lambda_body_ref(e, name, bound) for e in node.elements)
    if isinstance(node, ObjectConstructor):
        return any(
            _lambda_body_ref(p.key, name, bound) or _lambda_body_ref(p.value, name, bound) for p in node.pairs
        )
    if isinstance(node, PredicateExpr):
        return _lambda_body_ref(node.source, name, bound) or _lambda_body_ref(node.predicate, name, bound)
    if isinstance(node, Parenthesized):
        return _lambda_body_ref(node.inner, name, bound)
    return False


def _path_has_wildcard(path: PathExpr) -> bool:
    """True when path contains a WildcardStep or DescendantStep.

    Such paths must NOT be flattened into an outer path because the reference
    implementation calls evaluatePath for the inner sub-expression, which
    re-splits a plain input array into its elements before the wildcard step
    runs. Flattening removes that boundary, causing the wrong element-nesting
    level to reach the wildcard.
    """
    return any(isinstance(s, (WildcardStep, DescendantStep)) for s in path.steps)
