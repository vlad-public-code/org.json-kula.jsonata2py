"""Parses JSONata function signature strings and coerces argument lists to
match.

Ported from org.json_kula.jsonata_jvm.runtime.FunctionSignature.

Signature format: <params:return> where params is a sequence of type specs
and return is a single type symbol (may be absent).

Coercions applied:
  n -- number: string digits are parsed; booleans become 0/1
  s -- string: any scalar is stringified
  b -- boolean: any value is cast via JSONata truthy rules
  l -- null: value must be null or an exception is raised
  a -- array: a non-array scalar is wrapped in a single-element array
  o -- object: value must be an object or an exception is raised
  f -- function: value must be a function value (JLambda) or T0410 is
       raised. A parametrised form (f<n:n>) parses, but the parameter and
       return types of the argument function are not checked -- nor are
       they in jsonata-js.
  x -- any type at all, functions included; accepted without coercion
  j, u, unions, parametrised arrays -- accepted without coercion

Note: j is documented by the JSONata spec as excluding functions, but is
accepted here without that check -- tightening it would break working code
for no gain. Declare f to require a function.

Modifiers:
  + -- variadic: the type spec matches one or more trailing arguments
  ? -- optional: if missing, MISSING is passed through
  - -- focus: like optional; the caller supplies the context value if missing

If the signature cannot be parsed the argument list is passed through
unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from ..errors import _RuntimeEvaluationError as RuntimeEvaluationError
from . import core as _core
from .values import MISSING, UNKNOWN_ARITY, is_function

if TYPE_CHECKING:
    from ..bindings import JsonataFunctionArguments


class BoundFunction(Protocol):
    """Structural contract for a function bound via bindings.py (Phase 6).
    Mirrors JsonataBoundFunction -- apply() receives the JsonataFunctionArguments
    wrapper, not a raw list (lambdas.call_bound_function_value constructs one)."""

    def get_function_signature(self) -> str | None: ...
    def apply(self, args: JsonataFunctionArguments) -> Any: ...


# =============================================================================
# Public entry point
# =============================================================================


def coerce(signature: str | None, supplied: list[Any]) -> list[Any]:
    """Validates and coerces supplied against the parameter specs in
    signature, returning the coerced list.

    If the signature is None, empty, or malformed, supplied is returned
    unchanged so that bound functions that don't care about strict typing
    continue to work.
    """
    params = parse_params(signature)
    if params is None:
        return supplied

    result: list[Any] = []
    arg_idx = 0

    for p in params:
        if p.variadic:
            if arg_idx >= len(supplied):
                if not p.accepts_missing():
                    raise RuntimeEvaluationError(None, f'Expected at least one argument of type "{p.type}"')
                break
            while arg_idx < len(supplied):
                result.append(_coerce_one(p.type, supplied[arg_idx]))
                arg_idx += 1
        else:
            arg = supplied[arg_idx] if arg_idx < len(supplied) else MISSING
            arg_idx += 1
            if arg is MISSING:
                if p.accepts_missing():
                    result.append(MISSING)
                else:
                    raise RuntimeEvaluationError(None, f'Missing required argument of type "{p.type}"')
            else:
                result.append(_coerce_one(p.type, arg))

    return result


# =============================================================================
# Type coercion
# =============================================================================


def _coerce_one(type_: str, value: Any) -> Any:
    if value is MISSING:
        return value
    if type_[0] == "f":
        if not is_function(value):
            raise RuntimeEvaluationError("T0410", f"Expected function argument, got {_core._kind(value)}")
        return value
    if type_ == "n":
        return _core.num_node(_core.to_number(value))
    if type_ == "s":
        return _core.to_text(value)
    if type_ == "b":
        return _core.is_truthy(value)
    if type_ == "l":
        if value is not None:
            raise RuntimeEvaluationError(None, f"Expected null argument, got {_core._kind(value)}")
        return value
    if type_ == "a":
        return value if isinstance(value, list) else [value]
    if type_ == "o":
        if not isinstance(value, dict):
            raise RuntimeEvaluationError(None, f"Expected object argument, got {_core._kind(value)}")
        return value
    # j (any JSON), u (primitive union), x (any), union types (sao) etc.,
    # parametrised arrays a<x> -- accept without coercion
    return value


# =============================================================================
# Signature parsing
# =============================================================================


@dataclass(frozen=True, slots=True)
class ParamSpec:
    """Descriptor for a single parameter position extracted from a
    signature."""

    type: str
    optional: bool
    focus: bool
    variadic: bool

    def accepts_missing(self) -> bool:
        return self.optional or self.focus


def parse_params(signature: str | None) -> list[ParamSpec] | None:
    """Parses the params portion of signature into a list of ParamSpecs,
    or returns None if the signature is None, too short, or malformed."""
    if signature is None or len(signature) < 2:
        return None
    if signature[0] != "<" or signature[-1] != ">":
        return None

    inner = signature[1:-1]
    colon = _find_top_level_colon(inner)
    param_str = inner[:colon] if colon >= 0 else inner

    return _parse_param_str(param_str)


def _match_angle(s: str, open_: int) -> int:
    """Returns the index of the '>' closing the '<' at open_, or -1 if
    unbalanced."""
    depth = 0
    for i in range(open_, len(s)):
        c = s[i]
        if c == "<":
            depth += 1
        elif c == ">":
            depth -= 1
            if depth == 0:
                return i
    return -1


def arity_of(signature: str | None) -> int:
    """The number of arguments a function declaring signature takes, or
    UNKNOWN_ARITY when the signature does not pin it down."""
    params = parse_params(signature)
    if params is None:
        return UNKNOWN_ARITY
    for p in params:
        if p.variadic:
            return UNKNOWN_ARITY
    return len(params)


def _find_top_level_colon(s: str) -> int:
    depth = 0
    for i, c in enumerate(s):
        if c in ("(", "<"):
            depth += 1
        elif c in (")", ">"):
            depth -= 1
        elif c == ":" and depth == 0:
            return i
    return -1


_SIMPLE_TYPE_CHARS = "bnslaoufjx"


def _parse_param_str(s: str) -> list[ParamSpec] | None:
    result: list[ParamSpec] = []
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c == "(":
            end = s.find(")", i)
            if end < 0:
                return None
            type_ = s[i : end + 1]
            consumed = end + 1 - i
        elif c in ("a", "f") and i + 1 < n and s[i + 1] == "<":
            end = _match_angle(s, i + 1)
            if end < 0:
                return None
            type_ = s[i : end + 1]
            consumed = end + 1 - i
        elif c in _SIMPLE_TYPE_CHARS:
            type_ = c
            consumed = 1
        else:
            return None  # unexpected character -- treat as unparseable

        i += consumed

        optional = False
        focus = False
        variadic = False
        if i < n:
            mod = s[i]
            if mod == "+":
                variadic = True
                i += 1
            elif mod == "?":
                optional = True
                i += 1
            elif mod == "-":
                focus = True
                i += 1

        result.append(ParamSpec(type_, optional, focus, variadic))
    return result
