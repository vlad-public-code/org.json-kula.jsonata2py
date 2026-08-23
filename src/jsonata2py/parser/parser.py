"""Recursive-descent parser for the JSONata expression language.

Ported from org.json_kula.jsonata_jvm.parser.Parser.

Operator precedence (low -> high):
    1. :=   -- variable binding
    2. ?:   -- conditional (ternary)
    3. or
    4. and
    5. in   -- containment
    6. =  != <  <=  >  >=  -- comparison
    7. &    -- string concatenation
    8. +  -
    9. *  /  %
   10. Unary - and not
   11. ~>   -- function chaining
   12. Postfix: . path step, [predicate], ^(...) sort, {key:val} group-by,
       |...| transform

Usage:
    ast = Parser.parse("Account.Name")
"""

from __future__ import annotations

from ..errors import ParseError
from .ast_nodes import (
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
    WildcardStep,
)
from .lexer import Lexer
from .tokens import Token, TokenType

# Names of built-in functions (without $ prefix). Partial application of these via
# bare identifier syntax (no $) throws T1007; unknown names throw T1008.
BUILTIN_NAMES: frozenset[str] = frozenset(
    {
        "string", "length", "substring", "substringBefore", "substringAfter",
        "uppercase", "lowercase", "trim", "pad", "contains", "split", "join",
        "replace", "match", "number", "abs", "floor", "ceil", "round", "sqrt",
        "power", "random", "boolean", "not", "exists", "count", "sum", "max",
        "min", "average", "reverse", "sort", "shuffle", "distinct", "append",
        "keys", "lookup", "spread", "merge", "each", "sift", "type", "map",
        "filter", "reduce", "single", "zip", "formatNumber", "parseNumber",
        "formatBase", "formatInteger", "parseInteger", "now", "millis",
        "fromMillis", "toMillis", "error", "assert", "typeOf", "eval",
        "encodeUrl", "encodeUrlComponent", "decodeUrl", "decodeUrlComponent",
        "base64encode", "base64decode",
    }
)


# Binary operator precedence for _parse_binary, loosest first. The numbers
# are the levels the old recursive cascade encoded structurally: or < and
# < in < comparison < & < +- < */%. Unary minus, ~> chaining and the
# postfix steps bind tighter still and stay in their own functions.
_LOWEST_BINARY_BP = 1
_BINARY_OPS: dict[TokenType, tuple[int, str]] = {
    TokenType.OR: (1, "or"),
    TokenType.AND: (2, "and"),
    TokenType.IN: (3, "in"),
    TokenType.EQUAL: (4, "="),
    TokenType.NOT_EQUAL: (4, "!="),
    TokenType.LESS: (4, "<"),
    TokenType.LESS_EQUAL: (4, "<="),
    TokenType.GREATER: (4, ">"),
    TokenType.GREATER_EQUAL: (4, ">="),
    TokenType.AMPERSAND: (5, "&"),
    TokenType.PLUS: (6, "+"),
    TokenType.MINUS: (6, "-"),
    TokenType.STAR: (7, "*"),
    TokenType.SLASH: (7, "/"),
    TokenType.PERCENT: (7, "%"),
}

_TOKEN_TYPE_TO_SIG_CHAR = {
    TokenType.LESS: "<",
    TokenType.GREATER: ">",
    TokenType.LPAREN: "(",
    TokenType.RPAREN: ")",
    TokenType.COLON: ":",
    TokenType.PLUS: "+",
    TokenType.QUESTION: "?",
    TokenType.MINUS: "-",
}

_INVALID_SIG_PATTERN_CHARS = frozenset("bnslu")


def is_builtin(name: str) -> bool:
    """Returns True if name (without the leading $) is a JSONata built-in.

    Used by callers that need to tell a reference to the standard
    library from a reference to something the expression is expected
    to provide.
    """
    return name in BUILTIN_NAMES


class Parser:
    def __init__(self, tokens: list[Token]) -> None:
        self.tokens = tokens
        self.cursor = 0
        # When > 0, the postfix '|' (transform-as-postfix-operator) is suppressed.
        # This prevents the separator '|' between pattern and update inside a
        # transform literal from being greedily consumed as a nested transform
        # postfix operator.
        self.transform_pattern_depth = 0
        # Counter for generating unique temp variable names in lambda desugaring.
        self.lambda_temp_counter = 0

    @staticmethod
    def parse(expression: str) -> AstNode:
        """Parses expression and returns the root AST node."""
        tokens = Lexer.tokenize(expression)
        parser = Parser(tokens)
        result = parser._parse_expression()
        if parser._peek().type != TokenType.EOF:
            t = parser._peek()
            if t.type == TokenType.COLON_ASSIGN:
                raise ParseError("S0212", "The := operator can only be used to assign to a $variable", t.position)
            if t.type == TokenType.SEMICOLON:
                raise ParseError("S0201", "Syntax error: unexpected ';'", t.position)
            if t.type == TokenType.LPAREN:
                # A non-function expression followed by '(...)' -- detect
                # partial-application vs call
                inner = parser._peek_at(1)
                if inner.type == TokenType.QUESTION:
                    raise ParseError("T1008", "The expression is not a function", t.position)
                raise ParseError("T1006", "The expression is not a function", t.position)
            raise ParseError("S0211", f"Unexpected token '{t.value}'", t.position)
        return result

    # =========================================================================
    # Precedence levels
    # =========================================================================

    def _parse_expression(self) -> AstNode:
        """Entry point: lowest-precedence expression."""
        return self._parse_binding()

    def _parse_binding(self) -> AstNode:
        """Level 1: variable binding $name := expr (right-associative for
        chained :=)."""
        if self._peek().type == TokenType.VARIABLE and self._peek_at(1).type == TokenType.COLON_ASSIGN:
            name = self._consume(TokenType.VARIABLE).value
            self._consume(TokenType.COLON_ASSIGN)
            value = self._parse_binding()  # right-associative: $a := $b := 5 -> $a := ($b := 5)
            return VariableBinding(name, value)
        lhs = self._parse_conditional()
        # Detect invalid assignment target like $a[1]:=3 or foo:=3
        if self._peek().type == TokenType.COLON_ASSIGN:
            raise ParseError(
                "S0212", "The := operator can only be used to assign to a $variable", self._peek().position
            )
        return lhs

    def _parse_conditional(self) -> AstNode:
        """Level 2: conditional condition ? then : else / left ?: right /
        left ?? right.

        The ternary operator is right-associative so that chained
        ternaries like $x > 0 ? "pos" : $x < 0 ? "neg" : "zero" parse as
        ($x > 0 ? "pos" : ($x < 0 ? "neg" : "zero")). Both the 'then' and
        'otherwise' branches are parsed via a recursive call to
        _parse_conditional() rather than _parse_binary() to achieve this.
        """
        left = self._parse_binary()
        if self._peek().type == TokenType.QUESTION:
            self._consume(TokenType.QUESTION)
            then = self._parse_conditional()  # right-associative
            otherwise: AstNode | None = None
            if self._peek().type == TokenType.COLON:
                self._consume(TokenType.COLON)
                otherwise = self._parse_conditional()  # right-associative
            return ConditionalExpr(left, then, otherwise)
        if self._peek().type == TokenType.QUESTION_COLON:
            self._consume(TokenType.QUESTION_COLON)
            right = self._parse_conditional()
            return ElvisExpr(left, right)
        if self._peek().type == TokenType.QUESTION_QUESTION:
            self._consume(TokenType.QUESTION_QUESTION)
            right = self._parse_conditional()
            return CoalesceExpr(left, right)
        return left

    def _parse_binary(self, min_bp: int = _LOWEST_BINARY_BP) -> AstNode:
        """Levels 3-9: every left-associative binary operator, by
        precedence climbing.

        This replaces what were seven single-purpose recursive levels --
        or, and, in, comparison, &, +/-, and * / % -- each of which
        normally did nothing but parse the next level down, look at one
        token, and return. Profiling showed a near-identical call count at
        every one of those levels, which is the signature of pure
        delegation: roughly 4 800 frames per compile that only existed to
        encode precedence a table can encode instead.

        Associativity is in the recursive call: parsing the right operand
        at bp + 1 means an operator of the *same* precedence stops the
        recursion and is picked up by the loop below, which is what makes
        a - b - c parse as (a - b) - c. Operators of strictly tighter
        precedence still bind into the right operand, so the relative
        precedence of, say, `in` and `=` is preserved exactly.
        """
        left = self._parse_unary()
        tokens = self.tokens
        while True:
            token = tokens[self.cursor]
            entry = _BINARY_OPS.get(token.type)
            if entry is None:
                return left
            bp, default_op = entry
            if bp < min_bp:
                return left
            self.cursor += 1
            # Keywords (and, or, in) carry their text as the token value;
            # punctuation tokens carry "" and take the table's spelling.
            left = BinaryOp(token.value or default_op, left, self._parse_binary(bp + 1))

    def _parse_unary(self) -> AstNode:
        """Level 10: unary - and not."""
        if self._peek().type == TokenType.MINUS:
            self._consume(TokenType.MINUS)
            # Negative number literal optimisation
            if self._peek().type == TokenType.NUMBER:
                v = _parse_double(self._peek().value, self._peek().position)
                self.cursor += 1
                return NumberLiteral(-v)
            return UnaryMinus(self._parse_unary())
        if self._peek().type == TokenType.NOT:
            self._consume(TokenType.NOT)
            # 'not' is treated as a built-in single-argument function
            self._consume(TokenType.LPAREN)
            arg = self._parse_expression()
            self._consume(TokenType.RPAREN)
            return FunctionCall("not", [arg])
        return self._parse_chain()

    def _parse_chain(self) -> AstNode:
        """Level 11: ~> function chaining."""
        left = self._parse_postfix()
        if self._peek().type == TokenType.TILDE_GT:
            steps: list[AstNode] = [left]
            while self._peek().type == TokenType.TILDE_GT:
                self._consume(TokenType.TILDE_GT)
                steps.append(self._parse_postfix())
            return ChainExpr(steps)
        return left

    def _parse_postfix(self) -> AstNode:
        """Level 12: postfix -- . [] ^() {} ||."""
        node = self._parse_primary()

        while True:
            if self._peek().type == TokenType.DOT:
                node = self._parse_dot_step(node)
            elif self._peek().type == TokenType.AT and self._peek_at(1).type == TokenType.VARIABLE:
                # Context variable binding: node@$var
                # S0215: @$var cannot follow a predicate/subscript step
                if _ends_with_predicate_or_subscript(node):
                    t = self._peek()
                    raise ParseError(
                        "S0215",
                        "A context variable binding must not be applied after a predicate step",
                        t.position,
                    )
                # S0216: @$var cannot follow a sort expression
                if isinstance(node, SortExpr):
                    t = self._peek()
                    raise ParseError(
                        "S0216", "A context variable binding cannot follow a sort expression", t.position
                    )
                var_name = self._peek_at(1).value
                self.cursor += 2  # consume AT and VARIABLE
                node = _append_to_path(node, ContextBinding(var_name))
            elif self._peek().type == TokenType.AT:
                # AT not followed by $var -- must use a variable name (S0214)
                t = self._peek()
                raise ParseError("S0214", "The operand of the '@' operator must be a variable name ($var)", t.position)
            elif self._peek().type == TokenType.HASH and self._peek_at(1).type == TokenType.VARIABLE:
                # Positional variable binding: node#$var
                var_name = self._peek_at(1).value
                self.cursor += 2  # consume HASH and VARIABLE
                node = _append_to_path(node, PositionBinding(var_name))
            elif self._peek().type == TokenType.HASH:
                # HASH not followed by $var -- must use a variable name (S0214)
                t = self._peek()
                raise ParseError("S0214", "The operand of the '#' operator must be a variable name ($var)", t.position)
            elif self._peek().type == TokenType.LBRACKET:
                node = self._parse_subscript_or_predicate(node)
            elif self._peek().type == TokenType.CARET:
                node = self._parse_sort_expr(node)
            elif self._peek().type == TokenType.LBRACE:
                node = self._parse_group_by(node)
            elif self._peek().type == TokenType.PIPE and self.transform_pattern_depth == 0:
                node = self._parse_transform(node)
            elif self._peek().type == TokenType.LPAREN:
                # Chained function application: expr(args) where expr evaluates
                # to a lambda. Desugar to ($__callN := callee; $__callN(args))
                # so the translator can apply the result of any expression as a
                # function.
                node = self._desugar_call_expr(node)
            else:
                break
        return node

    # =========================================================================
    # Postfix helpers
    # =========================================================================

    def _parse_dot_step(self, left: AstNode) -> AstNode:
        self._consume(TokenType.DOT)
        # % after a dot means "parent step"
        if self._peek().type == TokenType.PERCENT:
            self.cursor += 1
            right: AstNode = ParentStep()
        elif self._peek().type == TokenType.STRING:
            # Quoted string after dot is a field name (e.g. Other."Alternative.Address")
            t = self._consume(TokenType.STRING)
            right = FieldRef(t.value)
        elif self._peek().type == TokenType.NUMBER:
            # A number literal after '.' is not a valid path step -- S0213
            t = self._peek()
            raise ParseError(
                "S0213",
                "The expression on the right side of the '.' operator must be a name or a wildcard, not a number",
                t.position,
            )
        else:
            right = self._parse_primary()
        # Flatten consecutive dot-steps into a single PathExpr
        steps: list[AstNode] = list(left.steps) if isinstance(left, PathExpr) else [left]
        steps.append(right)
        return PathExpr(steps)

    def _parse_subscript_or_predicate(self, source: AstNode) -> AstNode:
        if isinstance(source, GroupByExpr):
            t = self._peek()
            raise ParseError("S0209", "A predicate cannot be applied to a group-by expression", t.position)
        self._consume(TokenType.LBRACKET)
        if self._peek().type == TokenType.RBRACKET:
            # Empty [] -- force-array operator: wraps the result in an array
            self._consume(TokenType.RBRACKET)
            return ForceArray(source)
        # Range expression inside brackets: [from..to]
        inner = self._parse_expression()
        if self._peek().type == TokenType.DOT_DOT:
            self._consume(TokenType.DOT_DOT)
            to = self._parse_expression()
            self._consume(TokenType.RBRACKET)
            return PredicateExpr(source, RangeExpr(inner, to))
        self._consume(TokenType.RBRACKET)
        if isinstance(inner, NumberLiteral):
            # a.b[n] -- fold the subscript into the last path step so it is
            # applied per-element when the path maps over a sequence.
            # (a.b)[n] has source=Parenthesized and is NOT folded; the subscript
            # is applied to the whole collected result instead.
            if isinstance(source, PathExpr):
                steps = list(source.steps)
                last_step = steps.pop()
                steps.append(ArraySubscript(last_step, inner))
                return PathExpr(steps)
            # a.b.c[pred][n] -- fold when source is a PredicateExpr on a PathExpr,
            # so [n] is applied per-element (per-b), not on the globally collected
            # result.
            if isinstance(source, PredicateExpr) and isinstance(source.source, PathExpr):
                pp = source.source
                steps = list(pp.steps)
                last_step = steps.pop()
                steps.append(ArraySubscript(PredicateExpr(last_step, source.predicate), inner))
                return PathExpr(steps)
            return ArraySubscript(source, inner)
        # Non-numeric predicate: for positional-binding or context-binding paths
        # (ending in #$var or @$var), fold the predicate as a path step so that
        # $i / $var is in scope during filtering.
        if isinstance(source, PathExpr):
            path_steps = source.steps
            last_step = path_steps[-1]
            if isinstance(last_step, (PositionBinding, ContextBinding)):
                # Fold as a PredicateExpr path step so the binding is traversed first
                steps = list(path_steps)
                steps.append(PredicateExpr(ContextRef(), inner))
                return PathExpr(steps)
        return PredicateExpr(source, inner)

    def _parse_sort_expr(self, source: AstNode) -> AstNode:
        self._consume(TokenType.CARET)
        self._consume(TokenType.LPAREN)
        keys: list[SortKey] = []
        while True:
            descending = False  # Default: ascending sort
            if self._peek().type == TokenType.LESS:
                self._consume(TokenType.LESS)
                descending = False  # '<' prefix for ascending sort (descending=False)
            elif self._peek().type == TokenType.GREATER:
                self._consume(TokenType.GREATER)
                descending = True  # '>' prefix for descending sort
            keys.append(SortKey(self._parse_expression(), descending))
            if not self._try_consume(TokenType.COMMA):
                break
        self._consume(TokenType.RPAREN)
        return SortExpr(source, keys)

    def _parse_group_by(self, source: AstNode) -> AstNode:
        if isinstance(source, GroupByExpr):
            t = self._peek()
            raise ParseError("S0210", "Each group-by clause can only contain one expression", t.position)
        pairs = self._parse_object_body()
        return GroupByExpr(source, pairs)

    def _parse_transform(self, source: AstNode) -> AstNode:
        self._consume(TokenType.PIPE)
        # Suppress postfix-| inside the transform body so that the separator '|'
        # is not greedily consumed as a nested transform postfix operator.
        self.transform_pattern_depth += 1
        pattern = self._parse_expression()
        self._consume(TokenType.PIPE)
        # Update may be any expression; T2011 is raised at runtime if it's not an object.
        update = self._parse_expression()
        delete: AstNode | None = None
        if self._try_consume(TokenType.COMMA):
            delete = self._parse_expression()
        self.transform_pattern_depth -= 1
        self._consume(TokenType.PIPE)
        return TransformExpr(source, pattern, update, delete)

    def _parse_transform_lambda(self) -> AstNode:
        """Standalone transform literal: | pattern | update [, delete] |."""
        self._consume(TokenType.PIPE)
        # Suppress postfix-| inside the transform body so that the separator '|'
        # is not greedily consumed as a nested transform postfix operator.
        self.transform_pattern_depth += 1
        pattern = self._parse_expression()
        self._consume(TokenType.PIPE)
        # Update may be any expression; T2011 is raised at runtime if it's not an object.
        update = self._parse_expression()
        delete: AstNode | None = None
        if self._try_consume(TokenType.COMMA):
            delete = self._parse_expression()
        self.transform_pattern_depth -= 1
        self._consume(TokenType.PIPE)
        return TransformLambda(pattern, update, delete)

    # =========================================================================
    # Primary expressions
    # =========================================================================

    def _parse_primary(self) -> AstNode:
        t = self._peek()
        tt = t.type
        if tt == TokenType.STRING:
            self.cursor += 1
            return StringLiteral(t.value)
        if tt == TokenType.NUMBER:
            self.cursor += 1
            return NumberLiteral(_parse_double(t.value, t.position))
        if tt == TokenType.TRUE:
            self.cursor += 1
            return BooleanLiteral(True)
        if tt == TokenType.FALSE:
            self.cursor += 1
            return BooleanLiteral(False)
        if tt == TokenType.NULL:
            self.cursor += 1
            return NullLiteral()
        if tt == TokenType.REGEX:
            self.cursor += 1
            sep = t.value.rindex("/")
            return RegexLiteral(t.value[:sep], t.value[sep + 1 :])
        if tt == TokenType.DOLLAR_DOLLAR:
            self.cursor += 1
            return RootRef()
        if tt == TokenType.DOLLAR:
            self.cursor += 1
            return ContextRef()
        if tt == TokenType.VARIABLE:
            return self._parse_variable_or_function_call()
        if tt == TokenType.IDENTIFIER:
            return self._parse_identifier_or_function_call()
        if tt in (TokenType.AND, TokenType.OR, TokenType.IN):
            self.cursor += 1
            return FieldRef(t.value)
        if tt == TokenType.STAR:
            self.cursor += 1
            return WildcardStep()
        if tt == TokenType.STAR_STAR:
            self.cursor += 1
            return DescendantStep()
        if tt == TokenType.PERCENT:
            self.cursor += 1
            return ParentStep()
        if tt == TokenType.LPAREN:
            return self._parse_parenthesised()
        if tt == TokenType.LBRACKET:
            return self._parse_array_constructor()
        if tt == TokenType.LBRACE:
            return self._parse_object_constructor_node()
        if tt == TokenType.PIPE:
            return self._parse_transform_lambda()
        if tt == TokenType.QUESTION:
            self.cursor += 1
            # '?' followed by '(' is a lambda shorthand (same as 'function'/lambda)
            if self._peek().type == TokenType.LPAREN:
                lambda_node = self._parse_lambda()
                if self._peek().type == TokenType.LPAREN:
                    return self._desugar_immediate_lambda_call(lambda_node)
                return lambda_node
            return PartialPlaceholder()
        if tt == TokenType.MINUS:
            return self._parse_unary()  # let unary handle it
        if tt == TokenType.NOT:
            return self._parse_unary()
        if tt == TokenType.EOF:
            raise ParseError("S0207", "Unexpected end of expression", t.position)
        if tt == TokenType.ERROR:
            raise ParseError.with_error_code_from_message(t.value, t.position)
        raise ParseError("S0211", f"Unexpected token '{t.value}'", t.position)

    def _parse_variable_or_function_call(self) -> AstNode:
        """$name or $funcName(args)."""
        t = self._consume(TokenType.VARIABLE)
        if self._peek().type == TokenType.LPAREN:
            return self._parse_function_args(t.value, t.position)
        return VariableRef(t.value)

    def _parse_identifier_or_function_call(self) -> AstNode:
        """bareIdentifier, built-in function call, or lambda (function keyword)."""
        t = self._consume(TokenType.IDENTIFIER)
        # 'function' keyword (or Greek lambda) introduces a lambda expression
        if t.value in ("function", "λ") and self._peek().type == TokenType.LPAREN:
            lambda_node = self._parse_lambda()
            # Immediate invocation: function($x){body}(args) -- desugar to
            # ($__ln:=lambda; $__ln(args))
            if self._peek().type == TokenType.LPAREN:
                return self._desugar_immediate_lambda_call(lambda_node)
            return lambda_node
        if self._peek().type == TokenType.LPAREN:
            call = self._parse_function_args(t.value, t.position)
            if isinstance(call, PartialApplication):
                # Bare identifier partial application is invalid.
                # T1007 if name matches a known built-in; T1008 otherwise.
                if t.value in BUILTIN_NAMES:
                    raise ParseError(
                        "T1007", f"Attempted to partially apply a built-in function '{t.value}'", t.position
                    )
                raise ParseError("T1008", "The expression is not a function", t.position)
            # Regular call of a built-in function without $ prefix - raise T1005
            if t.value in BUILTIN_NAMES:
                raise ParseError(
                    "T1005", f"Attempted to invoke a non-function. Did you mean ${t.value}?", t.position
                )
            return call
        return FieldRef(t.value)

    def _parse_function_args(self, name: str, pos: int) -> AstNode:
        self._consume(TokenType.LPAREN)
        args: list[AstNode] = []
        if self._peek().type != TokenType.RPAREN:
            while True:
                args.append(self._parse_expression())
                if not self._try_consume(TokenType.COMMA):
                    break
        self._consume(TokenType.RPAREN)
        # If any argument is a PartialPlaceholder, produce a PartialApplication node.
        has_placeholder = any(isinstance(a, PartialPlaceholder) for a in args)
        if has_placeholder:
            return PartialApplication(name, args)
        return FunctionCall(name, args)

    def _parse_parenthesised(self) -> AstNode:
        """Parenthesised expression or block: (expr) or (expr; expr; ...)."""
        self._consume(TokenType.LPAREN)
        # Empty parens -- treat as empty block
        if self._peek().type == TokenType.RPAREN:
            self._consume(TokenType.RPAREN)
            return Block([])
        exprs: list[AstNode] = [self._parse_expression()]
        while self._peek().type == TokenType.SEMICOLON:
            self._consume(TokenType.SEMICOLON)
            if self._peek().type == TokenType.RPAREN:
                break  # trailing semicolon
            exprs.append(self._parse_expression())
        self._consume(TokenType.RPAREN)
        # Wrap in Parenthesized so that a following subscript [n] knows to apply
        # to the whole collected result rather than per path-step element.
        inner = exprs[0] if len(exprs) == 1 else Block(exprs)
        return Parenthesized(inner)

    def _parse_array_constructor(self) -> AstNode:
        """[elem, elem, ...] -- supports ranges and multi-range like [1..3, 7..9]."""
        self._consume(TokenType.LBRACKET)
        elements: list[AstNode] = []
        if self._peek().type != TokenType.RBRACKET:
            first = self._parse_expression()
            # Range element: from..to
            if self._peek().type == TokenType.DOT_DOT:
                self._consume(TokenType.DOT_DOT)
                to = self._parse_expression()
                elements.append(RangeExpr(first, to))
            else:
                elements.append(first)
            while self._try_consume(TokenType.COMMA):
                elem = self._parse_expression()
                if self._peek().type == TokenType.DOT_DOT:
                    self._consume(TokenType.DOT_DOT)
                    to = self._parse_expression()
                    elements.append(RangeExpr(elem, to))
                else:
                    elements.append(elem)
        self._consume(TokenType.RBRACKET)
        # Unwrap single range to preserve backward-compatible RangeExpr node
        if len(elements) == 1 and isinstance(elements[0], RangeExpr):
            return elements[0]
        return ArrayConstructor(elements)

    def _parse_object_constructor_node(self) -> AstNode:
        """{ key: value, ... }."""
        pairs = self._parse_object_body()
        return ObjectConstructor(pairs)

    def _parse_object_body(self) -> list[KeyValuePair]:
        self._consume(TokenType.LBRACE)
        pairs: list[KeyValuePair] = []
        if self._peek().type != TokenType.RBRACE:
            while True:
                key = self._parse_expression()
                self._consume(TokenType.COLON)
                value = self._parse_expression()
                pairs.append(KeyValuePair(key, value))
                if not self._try_consume(TokenType.COMMA):
                    break
        self._consume(TokenType.RBRACE)
        return pairs

    def _parse_lambda(self) -> AstNode:
        """Lambda: function($x, $y) { body }."""
        self._consume(TokenType.LPAREN)
        params: list[str] = []
        if self._peek().type != TokenType.RPAREN:
            while True:
                p = self._peek()
                if p.type != TokenType.VARIABLE:
                    raise ParseError("S0208", f"Lambda parameter '{p.value}' must be a $variable", p.position)
                self.cursor += 1
                params.append(p.value)
                if not self._try_consume(TokenType.COMMA):
                    break
        self._consume(TokenType.RPAREN)
        # Parse optional type-signature: <...> -- e.g. function($x,$y)<n-n:n>{body}
        signature: str | None = None
        if self._peek().type == TokenType.LESS:
            signature = self._read_type_signature()
        # Extra '>' after signature is S0402
        if self._peek().type == TokenType.GREATER:
            raise ParseError(
                "S0402", "Invalid type signature: unexpected '>' after signature", self._peek().position
            )
        self._consume(TokenType.LBRACE)
        body = self._parse_expression()
        self._consume(TokenType.RBRACE)
        return Lambda(params, body, signature)

    def _desugar_immediate_lambda_call(self, lambda_node: AstNode) -> AstNode:
        """Desugars an immediately-invoked lambda literal function($x){body}(args)
        to ($__ln_N := function($x){body}; $__ln_N(args))."""
        self._consume(TokenType.LPAREN)
        args: list[AstNode] = []
        if self._peek().type != TokenType.RPAREN:
            while True:
                args.append(self._parse_expression())
                if not self._try_consume(TokenType.COMMA):
                    break
        self._consume(TokenType.RPAREN)
        # If lambda has a signature, use LambdaCall so translator can apply
        # type-checking and context binding. Otherwise use the old desugared form.
        if isinstance(lambda_node, Lambda) and lambda_node.signature is not None:
            return LambdaCall(lambda_node, args)
        tmp_name = f"__ln_{self.lambda_temp_counter}"
        self.lambda_temp_counter += 1
        return Parenthesized(
            Block([VariableBinding(tmp_name, lambda_node), FunctionCall(tmp_name, args)])
        )

    def _desugar_call_expr(self, callee: AstNode) -> AstNode:
        """Desugars a chained call expr(args) (where expr is already parsed) to
        ($__callN := expr; $__callN(args)).

        Used in _parse_postfix to support calling the result of any
        expression as a function, e.g. $g($g)($a) or
        lambda($f){...}(arg1)(arg2).
        """
        self._consume(TokenType.LPAREN)
        args: list[AstNode] = []
        if self._peek().type != TokenType.RPAREN:
            while True:
                args.append(self._parse_expression())
                if not self._try_consume(TokenType.COMMA):
                    break
        self._consume(TokenType.RPAREN)
        tmp_name = f"__call_{self.lambda_temp_counter}"
        self.lambda_temp_counter += 1
        return Parenthesized(Block([VariableBinding(tmp_name, callee), FunctionCall(tmp_name, args)]))

    def _read_type_signature(self) -> str:
        """Reads and returns a type-signature string <...>, validating basic
        rules.

        Raises S0401 for invalid type specs (e.g. n<n>) and S0402 for
        invalid union types (e.g. (sa<n>) which has a typed array in a
        union without the outer close > in the right place).
        """
        start_pos = self._peek().position
        self.cursor += 1  # consume '<'
        sb: list[str] = ["<"]
        depth = 1
        while self.cursor < len(self.tokens) and depth > 0:
            tok = self.tokens[self.cursor]
            tt = tok.type
            sb.append(tok.value if tok.value else _TOKEN_TYPE_TO_SIG_CHAR.get(tt, ""))
            if tt == TokenType.LESS:
                depth += 1
            elif tt == TokenType.GREATER:
                depth -= 1
                if depth == 0:
                    break
            self.cursor += 1
        self.cursor += 1  # consume closing '>'
        sig = "".join(sb)
        # Validate: detect n<n> (parametrized non-array type) -> S0401
        if _has_invalid_parametrized_type(sig):
            raise ParseError("S0401", f"Invalid type specification in signature: '{sig}'", start_pos)
        return sig

    # =========================================================================
    # Token stream utilities
    # =========================================================================

    def _peek(self) -> Token:
        return self.tokens[self.cursor]

    def _peek_at(self, offset: int) -> Token:
        idx = self.cursor + offset
        return self.tokens[idx] if idx < len(self.tokens) else self.tokens[-1]

    def _consume(self, expected: TokenType) -> Token:
        t = self.tokens[self.cursor]
        if t.type != expected:
            if t.type == TokenType.EOF:
                raise ParseError(
                    "S0203", f"Expected {expected.name} but reached end of expression", t.position
                )
            raise ParseError(
                "S0202", f"Expected {expected.name} but found {t.type.name} ('{t.value}')", t.position
            )
        self.cursor += 1
        return t

    def _try_consume(self, type_: TokenType) -> bool:
        """Consumes the next token if it matches type_; returns True if consumed."""
        if self._peek().type == type_:
            self.cursor += 1
            return True
        return False


def _ends_with_predicate_or_subscript(node: AstNode) -> bool:
    """Returns True if the node's last path step is a predicate or numeric
    subscript."""
    last = node
    if isinstance(node, PathExpr) and node.steps:
        last = node.steps[-1]
    return isinstance(last, (PredicateExpr, ArraySubscript))


def _append_to_path(node: AstNode, step: AstNode) -> AstNode:
    """Appends step to node by extending or creating a PathExpr.

    If node is already a PathExpr, the step is added to its list.
    Otherwise, a new two-element PathExpr is created.
    """
    steps: list[AstNode] = list(node.steps) if isinstance(node, PathExpr) else [node]
    steps.append(step)
    return PathExpr(steps)


def _has_invalid_parametrized_type(sig: str) -> bool:
    """Mirrors the Java regex .*[bnslu]<.* used to detect n<n> style invalid
    parametrized non-array types."""
    return any(c in _INVALID_SIG_PATTERN_CHARS and i + 1 < len(sig) and sig[i + 1] == "<" for i, c in enumerate(sig))


def _parse_double(text: str, pos: int) -> float:
    try:
        return float(text)
    except ValueError:
        raise ParseError(None, f"Invalid number literal: {text}", pos) from None
