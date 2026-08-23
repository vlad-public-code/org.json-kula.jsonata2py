"""Turns a JSONata *definition expression* -- one that binds named lambdas
and returns the names of the ones to export -- into an expression that
also hands those lambdas back to the caller.

Ported from org.json_kula.jsonata_jvm.FunctionExportRewriter.

A definition expression is ordinary JSONata and evaluates on its own in
any JSONata engine; its result is the export list:

    (
      $pi := 3.14159;
      $product := function($a, $b) { $a * $b };
      $sin := function($x){ ... };
      $cos := function($x){ ... };
      ["sin", "cos"]                      <- the expression's own result
    )

The rewrite keeps that result and adds the values beside it. The trailing
expression is bound to a synthetic variable and a final object
constructor collects it together with every variable the definition
bound at its top level:

    (
      $pi := 3.14159; $product := ...; $sin := ...; $cos := ...;
      $__exportNames := ["sin", "cos"];
      {"names": $__exportNames,
       "functions": {"pi": $pi, "product": $product, "sin": $sin, "cos": $cos}}
    )

Collecting *all* top-level bindings is what allows a single compilation
and a single evaluation: which of them to export is only known once the
names expression has been evaluated, and by then the values are already
in hand.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import JsonataCompilationError
from .parser.ast_nodes import (
    AstNode,
    Block,
    KeyValuePair,
    ObjectConstructor,
    Parenthesized,
    StringLiteral,
    VariableBinding,
    VariableRef,
)
from .parser.parser import is_builtin
from .runtime import core as _core
from .runtime.values import MISSING
from .translator.scope_analyzer import free_variables

# Field of the export object holding the definition's own result -- the export list.
NAMES_FIELD = "names"
# Field of the export object holding every top-level binding, keyed by name.
FUNCTIONS_FIELD = "functions"

_NAMES_VAR_BASE = "__exportNames"


@dataclass(frozen=True, slots=True)
class ExportInfo:
    """What the export wrapper needs to know about one exported function.

    arity: the declared parameter count, or -1 when the binding's value
        is not a literal lambda (a partial application, a ~> chain, the
        result of a higher-order call...) and the arity is therefore only
        known at call time.
    signature: the JSONata signature to report to callers, or None for no
        validation or coercion at the Python boundary.
    """

    arity: int
    signature: str | None


def normalize(name: str) -> str:
    """Strips a leading $ so that a definition may list its exports either
    way. Map keys use the bare form, matching CompiledExpression.
    register_function and JsonataBindings.bind_function."""
    trimmed = name.strip()
    return trimmed[1:] if trimmed.startswith("$") else trimmed


def top_level_bindings(root: AstNode) -> dict[str, AstNode]:
    """Collects name -> value expression for every binding at the top
    level of root."""
    bindings: dict[str, AstNode] = {}
    unwrapped = root
    while isinstance(unwrapped, Parenthesized):
        unwrapped = unwrapped.inner
    expressions = unwrapped.expressions if isinstance(unwrapped, Block) else [unwrapped]
    for expr in expressions:
        # Chained assignment ($a := $b := value) binds every name in the
        # chain; a later re-binding of the same name wins, matching
        # evaluation order.
        current: AstNode = expr
        while isinstance(current, VariableBinding):
            bindings[current.name] = current.value
            current = current.value
    return bindings


def require_self_contained(root: AstNode, provided: set[str]) -> None:
    """Rejects a definition that refers to a name nothing provides.

    A definition is meant to be self-contained: whatever it uses, it
    either binds itself, gets from the JSONata standard library, or is
    handed at build time through JsonataLibraryOptions.bindings.
    Resolving the rest against whatever happens to be bound where an
    exported function is *called* would make the library's behaviour
    depend on its caller, and a typo indistinguishable from a deliberate
    hook -- so it is an error here, where the name and the fix can both
    be named.
    """
    unresolved = [name for name in free_variables(root) if name not in provided and not is_builtin(name)]
    if not unresolved:
        return
    one = len(unresolved) == 1
    raise JsonataCompilationError(
        None,
        "The definition expression uses $"
        + ", $".join(unresolved)
        + ", which it does not bind and which "
        + ("is not a JSONata built-in" if one else "are not JSONata built-ins")
        + ". Bind "
        + ("it" if one else "them")
        + " in the definition, or supply "
        + ("it" if one else "them")
        + " through JsonataLibraryOptions.bindings.",
    )


def rewrite(root: AstNode, bound_names: list[str]) -> AstNode:
    """Returns root rewritten to yield {"names": ..., "functions": {...}}."""
    names_var = _fresh_names_var(bound_names)

    function_pairs = [KeyValuePair(StringLiteral(name), VariableRef(name)) for name in bound_names]
    export_object = ObjectConstructor(
        [
            KeyValuePair(StringLiteral(NAMES_FIELD), VariableRef(names_var)),
            KeyValuePair(StringLiteral(FUNCTIONS_FIELD), ObjectConstructor(function_pairs)),
        ]
    )

    return _replace_tail(root, names_var, export_object)


def _replace_tail(root: AstNode, names_var: str, export_object: AstNode) -> AstNode:
    """Binds the definition's last expression to names_var and appends
    export_object after it, so the original result is computed exactly
    once and stays available."""
    if isinstance(root, Parenthesized):
        return Parenthesized(_replace_tail(root.inner, names_var, export_object))
    if isinstance(root, Block) and root.expressions:
        expressions = list(root.expressions)
        expressions[-1] = VariableBinding(names_var, expressions[-1])
        expressions.append(export_object)
        return Block(expressions)
    # A definition that is a single expression rather than a sequence,
    # e.g. just "['sin']" (which exports nothing and is reported as such)
    # or a lone binding.
    return Block([VariableBinding(names_var, root), export_object])


def _fresh_names_var(bound_names: list[str]) -> str:
    """Picks a synthetic variable name that the definition does not
    already bind."""
    taken = set(bound_names)
    candidate = _NAMES_VAR_BASE
    suffix = 2
    while candidate in taken:
        candidate = f"{_NAMES_VAR_BASE}{suffix}"
        suffix += 1
    return candidate


def exported_names(names: object) -> list[str]:
    """Reads the export list the definition returned: an array of
    strings, or a single string."""
    if names is None or names is MISSING:
        raise JsonataCompilationError(
            None, "The definition expression must return an array of function names to export, but it returned nothing"
        )
    elements = names if isinstance(names, list) else [names]
    if not elements:
        raise JsonataCompilationError(
            None, "The definition expression returned an empty array; it must name at least one function to export"
        )

    result: list[str] = []
    seen: set[str] = set()
    for element in elements:
        if not isinstance(element, str):
            raise JsonataCompilationError(
                None,
                "The definition expression must return an array of function names, but one element is "
                f"{_core.fn_type(element)}: {_core.sanitize_for_string(element)}",
            )
        name = normalize(element)
        if not name:
            raise JsonataCompilationError(None, "The definition expression returned an empty function name")
        if name in seen:
            raise JsonataCompilationError(None, f"The definition expression names ${name} twice")
        seen.add(name)
        result.append(name)
    return result


def describe(value: AstNode | None) -> ExportInfo:
    """Recovers the arity and signature of one exported function from the
    AST. value is the bound value expression, or None if the definition
    has no top-level binding of that name."""
    from .parser.ast_nodes import Lambda

    if isinstance(value, Lambda):
        arity = len(value.params)
        declared = value.signature
        return ExportInfo(arity, declared if declared is not None else _synthesize_signature(arity))
    # Computed function values -- $twice($add3), $uppercase ~> $trim,
    # $substring(?, 0, 5), a conditional choosing between two lambdas...
    # The value is checked for real after the definition runs; until then
    # neither arity nor signature is known.
    return ExportInfo(-1, None)


def _synthesize_signature(arity: int) -> str | None:
    """Builds an all-optional signature such as <j?j?:j>. Optional is
    deliberate: JSONata lets a lambda be called with fewer arguments than
    it declares, binding the rest to undefined, and j applies no
    coercion -- so the exported function accepts exactly what the same
    function accepts when called from JSONata."""
    if arity == 0:
        return None
    return "<" + "j?" * arity + ":j>"
