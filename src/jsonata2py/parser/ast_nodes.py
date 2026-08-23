"""AST node types for a parsed JSONata expression.

Ported from org.json_kula.jsonata_jvm.parser.ast.AstNode.

Java uses a sealed interface with 38 nested record types and a
two-parameter Visitor<R, C>. Python: 38 frozen, slotted dataclasses
sharing a common marker base class (so isinstance/match work), a
Visitor Protocol with the same 38 visit_* methods, and a module-level
accept(node, visitor, ctx) implemented with a match statement --
Python's structural pattern matching over a closed set of dataclasses
is the direct analogue of Java's exhaustive switch over a sealed
hierarchy.

frozen=True matters: the optimizer rebuilds nodes rather than
mutating, and the translator relies on nodes being safely shareable.
slots=True matters for memory on large expressions.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, TypeVar


class AstNode:
    """Marker base class for every AST node type. Empty __slots__ so
    subclasses using dataclass(slots=True) don't pay for a __dict__."""

    __slots__ = ()


# =============================================================================
# Literals
# =============================================================================


@dataclass(frozen=True, slots=True)
class StringLiteral(AstNode):
    """A string literal, e.g. "hello" or 'hello'."""

    value: str


@dataclass(frozen=True, slots=True)
class NumberLiteral(AstNode):
    """A numeric literal, e.g. 42, 3.14, 1e10."""

    value: float


@dataclass(frozen=True, slots=True)
class BooleanLiteral(AstNode):
    """true or false."""

    value: bool


@dataclass(frozen=True, slots=True)
class NullLiteral(AstNode):
    """The literal null."""


@dataclass(frozen=True, slots=True)
class RegexLiteral(AstNode):
    """A regex literal, e.g. /foo/i.

    Attributes:
        pattern: the regex pattern string (without the surrounding /)
        flags: the flags string (e.g. "i", "im", or "")
    """

    pattern: str
    flags: str


# =============================================================================
# References
# =============================================================================


@dataclass(frozen=True, slots=True)
class ContextRef(AstNode):
    """The bare $ token -- refers to the current context value (or marks a
    lambda parameter position)."""


@dataclass(frozen=True, slots=True)
class RootRef(AstNode):
    """The $$ token -- refers to the root of the input document."""


@dataclass(frozen=True, slots=True)
class VariableRef(AstNode):
    """A variable reference such as $name.

    Attributes:
        name: the variable name, without the leading $
    """

    name: str


# =============================================================================
# Path steps
# =============================================================================


@dataclass(frozen=True, slots=True)
class FieldRef(AstNode):
    """A bare field name in a path, e.g. Account in Account.Name.

    Attributes:
        name: the field name (may include any Unicode characters if
            backtick-quoted)
    """

    name: str


@dataclass(frozen=True, slots=True)
class WildcardStep(AstNode):
    """The * wildcard -- matches all fields of an object."""


@dataclass(frozen=True, slots=True)
class DescendantStep(AstNode):
    """The ** recursive-descent wildcard -- matches all descendants."""


@dataclass(frozen=True, slots=True)
class ParentStep(AstNode):
    """The % parent step in a path -- navigates to the parent object."""


@dataclass(frozen=True, slots=True)
class PositionBinding(AstNode):
    """Positional variable binding: expr#$var.

    Binds the 0-based index of each sequence element to var_name.
    Appears as a step inside a PathExpr.
    """

    var_name: str


@dataclass(frozen=True, slots=True)
class ContextBinding(AstNode):
    """Context variable binding: expr@$var.

    Binds the current context element to var_name for use in
    subsequent steps. Appears as a step inside a PathExpr.
    """

    var_name: str


# =============================================================================
# Constructors
# =============================================================================


@dataclass(frozen=True, slots=True)
class ArrayConstructor(AstNode):
    """Array constructor: [expr, expr, ...].

    Attributes:
        elements: the element expressions (may be empty)
    """

    elements: list[AstNode]


@dataclass(frozen=True, slots=True)
class KeyValuePair:
    """A single key-value pair inside an object constructor.

    Attributes:
        key: expression that produces the key string
        value: expression that produces the value
    """

    key: AstNode
    value: AstNode


@dataclass(frozen=True, slots=True)
class ObjectConstructor(AstNode):
    """Object constructor: { key: value, ... }.

    Attributes:
        pairs: the key-value pairs (may be empty)
    """

    pairs: list[KeyValuePair]


# =============================================================================
# Path expressions
# =============================================================================


@dataclass(frozen=True, slots=True)
class PathExpr(AstNode):
    """A chain of path steps separated by '.'.

    Each element is a path step: a FieldRef, WildcardStep,
    DescendantStep, PredicateExpr, ArraySubscript, or another nested
    expression.

    Attributes:
        steps: at least two steps
    """

    steps: list[AstNode]


@dataclass(frozen=True, slots=True)
class PredicateExpr(AstNode):
    """A predicate filter applied to a sequence: expr[predicate].

    Attributes:
        source: the input expression
        predicate: the filter condition (or an integer index)
    """

    source: AstNode
    predicate: AstNode


@dataclass(frozen=True, slots=True)
class ArraySubscript(AstNode):
    """An array subscript using the [index] notation where index is a
    numeric expression. Logically equivalent to a predicate but emitted
    separately for clarity during translation.

    Attributes:
        source: the input expression
        index: the numeric index expression
    """

    source: AstNode
    index: AstNode


# =============================================================================
# Operators
# =============================================================================


@dataclass(frozen=True, slots=True)
class BinaryOp(AstNode):
    """A binary infix operation such as a + b, a = b, a and b.

    Attributes:
        op: the operator string (e.g. "+", "and", "!=")
        left: left operand
        right: right operand
    """

    op: str
    left: AstNode
    right: AstNode


@dataclass(frozen=True, slots=True)
class UnaryMinus(AstNode):
    """Unary negation: -expr.

    Attributes:
        operand: the expression being negated
    """

    operand: AstNode


# =============================================================================
# Functions and lambdas
# =============================================================================


@dataclass(frozen=True, slots=True)
class FunctionCall(AstNode):
    """A function invocation: $func(arg1, arg2, ...).

    Attributes:
        name: the function name (without leading $)
        args: the argument expressions (may be empty)
    """

    name: str
    args: list[AstNode]


@dataclass(frozen=True, slots=True)
class Lambda(AstNode):
    """A lambda (anonymous function): function($x, $y) { body }.

    Attributes:
        params: parameter names without the leading $
        body: the function body
        signature: an optional <params:return> signature string
    """

    params: list[str]
    body: AstNode
    signature: str | None = None


@dataclass(frozen=True, slots=True)
class LambdaCall(AstNode):
    """An immediate lambda invocation with signature awareness:
    function($x,$y)<sig>{body}(args).

    Used when the lambda has a signature -- the translator uses this
    to emit type-checking and context-binding code.
    """

    lambda_: Lambda
    args: list[AstNode]


# =============================================================================
# Variable binding
# =============================================================================


@dataclass(frozen=True, slots=True)
class VariableBinding(AstNode):
    """A variable assignment: $name := expr.

    Attributes:
        name: the variable name without the leading $
        value: the expression whose result is bound
    """

    name: str
    value: AstNode


# =============================================================================
# Control flow
# =============================================================================


@dataclass(frozen=True, slots=True)
class ConditionalExpr(AstNode):
    """Conditional expression: condition ? then : else.

    The else branch is optional (None means absent).

    Attributes:
        condition: the guard expression
        then: the true branch
        otherwise: the false branch, or None if omitted
    """

    condition: AstNode
    then: AstNode
    otherwise: AstNode | None


@dataclass(frozen=True, slots=True)
class Block(AstNode):
    """A block of expressions enclosed in parentheses: (expr1; expr2; ...).

    Evaluates each expression in order; the block's value is the last
    expression.

    Attributes:
        expressions: at least one expression
    """

    expressions: list[AstNode]


# =============================================================================
# Range, sort, group-by
# =============================================================================


@dataclass(frozen=True, slots=True)
class RangeExpr(AstNode):
    """A range expression: [from..to].

    Attributes:
        from_: the lower bound
        to: the upper bound
    """

    from_: AstNode
    to: AstNode


@dataclass(frozen=True, slots=True)
class SortKey:
    """A single sort key inside a sort expression.

    Attributes:
        key: the expression used as sort key
        descending: True for descending (>), False for ascending (<)
    """

    key: AstNode
    descending: bool


@dataclass(frozen=True, slots=True)
class SortExpr(AstNode):
    """A sort expression: expr^(key1, >key2, ...).

    Attributes:
        source: the sequence to sort
        keys: the ordered list of sort criteria
    """

    source: AstNode
    keys: list[SortKey]


@dataclass(frozen=True, slots=True)
class GroupByExpr(AstNode):
    """A group-by / reduce expression: expr{key: value}.

    Attributes:
        source: the input sequence
        pairs: the aggregation key-value pairs
    """

    source: AstNode
    pairs: list[KeyValuePair]


# =============================================================================
# Chaining and transform
# =============================================================================


@dataclass(frozen=True, slots=True)
class ChainExpr(AstNode):
    """The pipe-chain operator: expr ~> $func.

    Applies each function in the chain to the result of the previous
    step.

    Attributes:
        steps: the ordered sequence of chain steps (left to right)
    """

    steps: list[AstNode]


@dataclass(frozen=True, slots=True)
class TransformExpr(AstNode):
    """A transform expression using the pipe (|) syntax:
    expr | pattern | update [, delete] |.

    Attributes:
        source: the source expression (the document to transform)
        pattern: the location path evaluated against the source
        update: the update object merged into each matched node
        delete: optional expression evaluating to a field name or
            array of field names to remove from each matched node;
            may be None
    """

    source: AstNode
    pattern: AstNode
    update: AstNode
    delete: AstNode | None


@dataclass(frozen=True, slots=True)
class TransformLambda(AstNode):
    """A standalone transform function literal: | pattern | update [, delete] |.

    When used in a chain (src ~> |pattern|update|) the chain operator
    passes the left-hand value as the source. The translator emits
    this node as a lambda value so that fn_pipe can invoke it.

    Attributes:
        pattern: the location path evaluated against the supplied source
        update: the update object merged into each matched node
        delete: optional delete-field expression; may be None
    """

    pattern: AstNode
    update: AstNode
    delete: AstNode | None


@dataclass(frozen=True, slots=True)
class ForceArray(AstNode):
    """The force-array postfix operator expr[].

    Forces the result of the path expression to be an array even when
    only one value was selected. Stands in contrast to JSONata's
    normal singleton-collapsing behaviour where a one-element sequence
    is returned as a bare value.

    Attributes:
        source: the expression whose result must be an array
    """

    source: AstNode


@dataclass(frozen=True, slots=True)
class ElvisExpr(AstNode):
    """Elvis / default operator: left ?: right.

    Returns left if truthy, otherwise right.
    """

    left: AstNode
    right: AstNode


@dataclass(frozen=True, slots=True)
class CoalesceExpr(AstNode):
    """Coalescing operator: left ?? right.

    Returns left if not missing, otherwise right.
    """

    left: AstNode
    right: AstNode


@dataclass(frozen=True, slots=True)
class PartialPlaceholder(AstNode):
    """Partial-application placeholder: the ? token inside a function call.

    Replaced at runtime by the argument supplied to the
    partially-applied function.
    """


@dataclass(frozen=True, slots=True)
class PartialApplication(AstNode):
    """A partial function application: $fn(arg1, ?, arg3, ...).

    Any argument that is a PartialPlaceholder will be supplied when
    the resulting function is invoked.

    Attributes:
        name: the function name (without leading $)
        args: the argument list, some of which may be PartialPlaceholder
    """

    name: str
    args: list[AstNode]


@dataclass(frozen=True, slots=True)
class Parenthesized(AstNode):
    """Marks an expression that was written inside explicit parentheses in
    the source.

    Parentheses are normally transparent (they don't change runtime
    semantics), but they do affect how a following subscript [n] is
    applied:
      - a.b[n] -- subscript applied per-element (bound to step b).
      - (a.b)[n] -- subscript applied to the whole collected sequence.

    Wrapping the inner expression in this node lets the parser record
    that the expression was parenthesised so that
    parse_subscript_or_predicate can choose the correct binding
    strategy. Not a no-op wrapper: the optimizer must not unwrap it.

    Attributes:
        inner: the wrapped expression
    """

    inner: AstNode


# =============================================================================
# Visitor
# =============================================================================

R = TypeVar("R")
C = TypeVar("C")

# The Visitor protocol returns R and consumes C, so within the protocol they
# must be covariant/contravariant. accept() below keeps the invariant pair:
# a plain function signature cannot use variant type variables.
R_co = TypeVar("R_co", covariant=True)
C_contra = TypeVar("C_contra", contravariant=True)


class Visitor(Protocol[R_co, C_contra]):
    """Two-parameter visitor over the full AstNode hierarchy.

    The context parameter C allows callers to thread arbitrary
    per-traversal state (symbol tables, type environments, output
    builders, etc.) without relying on mutable visitor fields.
    """

    def visit_string_literal(self, node: StringLiteral, ctx: C_contra) -> R_co: ...
    def visit_number_literal(self, node: NumberLiteral, ctx: C_contra) -> R_co: ...
    def visit_boolean_literal(self, node: BooleanLiteral, ctx: C_contra) -> R_co: ...
    def visit_null_literal(self, node: NullLiteral, ctx: C_contra) -> R_co: ...
    def visit_regex_literal(self, node: RegexLiteral, ctx: C_contra) -> R_co: ...
    def visit_context_ref(self, node: ContextRef, ctx: C_contra) -> R_co: ...
    def visit_root_ref(self, node: RootRef, ctx: C_contra) -> R_co: ...
    def visit_variable_ref(self, node: VariableRef, ctx: C_contra) -> R_co: ...
    def visit_field_ref(self, node: FieldRef, ctx: C_contra) -> R_co: ...
    def visit_wildcard_step(self, node: WildcardStep, ctx: C_contra) -> R_co: ...
    def visit_descendant_step(self, node: DescendantStep, ctx: C_contra) -> R_co: ...
    def visit_parent_step(self, node: ParentStep, ctx: C_contra) -> R_co: ...
    def visit_position_binding(self, node: PositionBinding, ctx: C_contra) -> R_co: ...
    def visit_context_binding(self, node: ContextBinding, ctx: C_contra) -> R_co: ...
    def visit_array_constructor(self, node: ArrayConstructor, ctx: C_contra) -> R_co: ...
    def visit_object_constructor(self, node: ObjectConstructor, ctx: C_contra) -> R_co: ...
    def visit_path_expr(self, node: PathExpr, ctx: C_contra) -> R_co: ...
    def visit_predicate_expr(self, node: PredicateExpr, ctx: C_contra) -> R_co: ...
    def visit_array_subscript(self, node: ArraySubscript, ctx: C_contra) -> R_co: ...
    def visit_binary_op(self, node: BinaryOp, ctx: C_contra) -> R_co: ...
    def visit_unary_minus(self, node: UnaryMinus, ctx: C_contra) -> R_co: ...
    def visit_function_call(self, node: FunctionCall, ctx: C_contra) -> R_co: ...
    def visit_lambda(self, node: Lambda, ctx: C_contra) -> R_co: ...
    def visit_variable_binding(self, node: VariableBinding, ctx: C_contra) -> R_co: ...
    def visit_conditional_expr(self, node: ConditionalExpr, ctx: C_contra) -> R_co: ...
    def visit_block(self, node: Block, ctx: C_contra) -> R_co: ...
    def visit_range_expr(self, node: RangeExpr, ctx: C_contra) -> R_co: ...
    def visit_sort_expr(self, node: SortExpr, ctx: C_contra) -> R_co: ...
    def visit_group_by_expr(self, node: GroupByExpr, ctx: C_contra) -> R_co: ...
    def visit_chain_expr(self, node: ChainExpr, ctx: C_contra) -> R_co: ...
    def visit_transform_expr(self, node: TransformExpr, ctx: C_contra) -> R_co: ...
    def visit_transform_lambda(self, node: TransformLambda, ctx: C_contra) -> R_co: ...
    def visit_parenthesized(self, node: Parenthesized, ctx: C_contra) -> R_co: ...
    def visit_force_array(self, node: ForceArray, ctx: C_contra) -> R_co: ...
    def visit_elvis_expr(self, node: ElvisExpr, ctx: C_contra) -> R_co: ...
    def visit_coalesce_expr(self, node: CoalesceExpr, ctx: C_contra) -> R_co: ...
    def visit_partial_placeholder(self, node: PartialPlaceholder, ctx: C_contra) -> R_co: ...
    def visit_partial_application(self, node: PartialApplication, ctx: C_contra) -> R_co: ...
    def visit_lambda_call(self, node: LambdaCall, ctx: C_contra) -> R_co: ...


# Dispatch table for accept(). The `match` statement it replaces was a
# linear scan of 40 class patterns: cheap for the arms near the top,
# measured at 947 ns for one near the bottom against 146 ns for a dict
# lookup. Built from the class object itself, so a node type added
# without a matching entry falls through to the explicit error below
# rather than being silently mis-dispatched.
_VISIT_METHOD: dict[type, str] = {
    StringLiteral: "visit_string_literal",
    NumberLiteral: "visit_number_literal",
    BooleanLiteral: "visit_boolean_literal",
    NullLiteral: "visit_null_literal",
    RegexLiteral: "visit_regex_literal",
    ContextRef: "visit_context_ref",
    RootRef: "visit_root_ref",
    VariableRef: "visit_variable_ref",
    FieldRef: "visit_field_ref",
    WildcardStep: "visit_wildcard_step",
    DescendantStep: "visit_descendant_step",
    ParentStep: "visit_parent_step",
    PositionBinding: "visit_position_binding",
    ContextBinding: "visit_context_binding",
    ArrayConstructor: "visit_array_constructor",
    ObjectConstructor: "visit_object_constructor",
    PathExpr: "visit_path_expr",
    PredicateExpr: "visit_predicate_expr",
    ArraySubscript: "visit_array_subscript",
    BinaryOp: "visit_binary_op",
    UnaryMinus: "visit_unary_minus",
    FunctionCall: "visit_function_call",
    Lambda: "visit_lambda",
    VariableBinding: "visit_variable_binding",
    ConditionalExpr: "visit_conditional_expr",
    Block: "visit_block",
    RangeExpr: "visit_range_expr",
    SortExpr: "visit_sort_expr",
    GroupByExpr: "visit_group_by_expr",
    ChainExpr: "visit_chain_expr",
    TransformExpr: "visit_transform_expr",
    TransformLambda: "visit_transform_lambda",
    Parenthesized: "visit_parenthesized",
    ForceArray: "visit_force_array",
    ElvisExpr: "visit_elvis_expr",
    CoalesceExpr: "visit_coalesce_expr",
    PartialPlaceholder: "visit_partial_placeholder",
    PartialApplication: "visit_partial_application",
    LambdaCall: "visit_lambda_call",
}


def accept(node: AstNode, visitor: Visitor[R, C], ctx: C) -> R:
    """Dispatches node to the appropriate visit_* method on visitor."""
    method = _VISIT_METHOD.get(node.__class__)
    if method is None:
        raise AssertionError(f"unhandled AST node: {type(node).__name__}")
    return getattr(visitor, method)(node, ctx)  # type: ignore[no-any-return]


AstNodeUnion = (
    StringLiteral
    | NumberLiteral
    | BooleanLiteral
    | NullLiteral
    | RegexLiteral
    | ContextRef
    | RootRef
    | VariableRef
    | FieldRef
    | WildcardStep
    | DescendantStep
    | ParentStep
    | PositionBinding
    | ContextBinding
    | ArrayConstructor
    | ObjectConstructor
    | PathExpr
    | PredicateExpr
    | ArraySubscript
    | Parenthesized
    | BinaryOp
    | UnaryMinus
    | FunctionCall
    | Lambda
    | VariableBinding
    | ConditionalExpr
    | Block
    | RangeExpr
    | SortExpr
    | GroupByExpr
    | ChainExpr
    | TransformExpr
    | TransformLambda
    | ForceArray
    | ElvisExpr
    | CoalesceExpr
    | PartialPlaceholder
    | PartialApplication
    | LambdaCall
)
