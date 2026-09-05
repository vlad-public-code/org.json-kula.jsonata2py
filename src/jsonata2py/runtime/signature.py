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

import re
from dataclasses import dataclass
from functools import lru_cache
from itertools import product
from typing import TYPE_CHECKING, Any, Protocol

from ..errors import _RuntimeEvaluationError as RuntimeEvaluationError
from . import core as _core
from .values import MISSING, UNKNOWN_ARITY, is_function, is_regex

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


# =============================================================================
# Built-in call validation (jsonata-js `parseSignature`)
# =============================================================================
#
# `coerce` above answers "adapt this argument list to this signature", which
# is what a *user-defined* function needs. A built-in needs a different
# question answered: "which parameter did each supplied argument land on, and
# is the evaluation context standing in for one of them?".
#
# The reference answers it by compiling the signature into a regular
# expression over one-character type symbols -- one capturing group per
# parameter -- and matching the symbols of the supplied arguments against it
# (jsonata.js `parseSignature`/`validate`). A parameter marked `-` has its
# group made optional, so when the arguments cannot fill it the group matches
# empty and the context value is substituted in its place.
#
# That indirection is why the decision cannot be made from the argument
# *count* alone, and so cannot be made at compile time:
#
#     (1.55).$round(1)      -> 1     the number fills `n-`; no substitution
#     (2).$power(3)         -> 8     `n-n` needs two; the context fills the first
#
# Both calls supply one argument. Only the parameter types tell them apart.


@dataclass(frozen=True, slots=True)
class _MatchParam:
    """One parameter position, with the symbol-regex fragment that matches
    the arguments it can consume."""

    regex: str
    type: str
    context: bool
    context_regex: re.Pattern[str] | None
    array: bool
    subtype: str | None


@dataclass(frozen=True, slots=True)
class _Matcher:
    params: tuple[_MatchParam, ...]
    regex: re.Pattern[str]


_ARRAY_TYPE_NAMES = {
    "a": "arrays",
    "b": "booleans",
    "f": "functions",
    "n": "numbers",
    "o": "objects",
    "s": "strings",
}


def _symbol(value: Any) -> str:
    """The one-character type symbol the reference's `getSymbol` assigns to
    a value. `m` marks an absent argument, which every type may match."""
    if value is MISSING:
        return "m"
    if is_function(value) or is_regex(value):
        return "f"
    if value is None:
        return "l"
    if value is True or value is False:
        return "b"
    if isinstance(value, str):
        return "s"
    if isinstance(value, (int, float)):
        return "n"
    if isinstance(value, list):
        return "a"
    return "o"


def _find_closing(s: str, start: int, open_: str, close: str) -> int:
    depth = 1
    pos = start
    while pos < len(s) - 1:
        pos += 1
        if s[pos] == close:
            depth -= 1
            if depth == 0:
                return pos
        elif s[pos] == open_:
            depth += 1
    return pos


@lru_cache(maxsize=128)
def _compile_matcher(signature: str) -> _Matcher | None:
    """Ports jsonata.js `parseSignature`. Returns None for a signature this
    port does not model, so callers fall back to their direct call."""
    parts: list[dict[str, Any]] = []
    pos = 1
    while pos < len(signature):
        c = signature[pos]
        if c == ":":
            break
        if c in ("s", "n", "b", "l", "o"):
            parts.append({"regex": f"[{c}m]", "type": c, "array": False})
        elif c == "a":
            parts.append({"regex": "[asnblfom]", "type": "a", "array": True})
        elif c == "f":
            parts.append({"regex": "f", "type": "f", "array": False})
        elif c == "j":
            parts.append({"regex": "[asnblom]", "type": "j", "array": False})
        elif c == "x":
            parts.append({"regex": "[asnblfom]", "type": "x", "array": False})
        elif c == "-":
            if not parts:
                return None
            prev = parts[-1]
            prev["context"] = True
            prev["context_regex"] = re.compile(prev["regex"])
            prev["regex"] += "?"
        elif c in ("?", "+"):
            if not parts:
                return None
            parts[-1]["regex"] += c
        elif c == "(":
            end = _find_closing(signature, pos, "(", ")")
            choice = signature[pos + 1 : end]
            if "<" in choice:
                return None  # parameterised unions -- S0402 in the reference
            parts.append({"regex": f"[{choice}m]", "type": f"({choice})", "array": False})
            pos = end
        elif c == "<":
            if not parts or parts[-1]["type"] not in ("a", "f"):
                return None
            end = _find_closing(signature, pos, "<", ">")
            parts[-1]["subtype"] = signature[pos + 1 : end]
            pos = end
        elif c == ">":
            pass  # the signature's own closing bracket
        else:
            return None
        pos += 1

    if not parts:
        return None
    params = tuple(
        _MatchParam(
            regex=p["regex"],
            type=p["type"],
            context=p.get("context", False),
            context_regex=p.get("context_regex"),
            array=p["array"],
            subtype=p.get("subtype"),
        )
        for p in parts
    )
    return _Matcher(params, re.compile("^" + "".join(f"({p.regex})" for p in params) + "$"))


def param_count(signature: str) -> int:
    """The number of parameter positions in signature, or -1 when it is not
    modelled. -2 marks a variadic signature, whose position count is
    unbounded."""
    m = _compile_matcher(signature)
    if m is None:
        return -1
    if any(p.regex.endswith("+") for p in m.params):
        return -2
    return len(m.params)


def arity_bounds(signature: str) -> tuple[int, int] | None:
    """(required, maximum) argument counts, or None when not modelled.

    A parameter is required unless it is optional (`?`) or can be filled by
    the evaluation context (`-`). `maximum` is -1 for a variadic signature.
    The reference reports a count outside these bounds as T0410, like any
    other signature mismatch.
    """
    if signature.startswith("<:"):
        # A signature with no parameters at all ($millis, $random): any
        # argument is one too many.
        return (0, 0)
    matcher = _compile_matcher(signature)
    if matcher is None:
        return None
    if any(p.regex.endswith("+") for p in matcher.params):
        return (0, -1)
    required = sum(
        1 for p in matcher.params if not p.regex.endswith("?") and not p.context
    )
    return (required, len(matcher.params))


def _raise_t0410(matcher: _Matcher, args: list[Any], supplied: str) -> None:
    """Reports which argument first failed to match, as the reference's
    `throwValidationError` does."""
    partial = ""
    good_to = 0
    for p in matcher.params:
        partial += p.regex
        m = re.match("^" + partial, supplied)
        if m is None:
            break
        good_to = len(m.group(0))
    raise RuntimeEvaluationError(
        "T0410", f"Argument {good_to + 1} of function does not match function signature"
    )


#: Python classes per type symbol, for the specialised checker. `n` excludes
#: bool deliberately -- `True.__class__` is `bool`, never `int`, so a class
#: membership test gets JSONata's number/boolean split right for free.
_CLASS_SYMBOL: dict[type, str] = {
    bool: "b",
    str: "s",
    int: "n",
    float: "n",
    list: "a",
    dict: "o",
    type(None): "l",
}


def _fast_symbol(value: Any) -> str:
    """`_symbol` without the isinstance chain, for the common classes."""
    sym = _CLASS_SYMBOL.get(value.__class__)
    return sym if sym is not None else _symbol(value)


@lru_cache(maxsize=512)
def _fixed_plan(signature: str, argc: int) -> tuple[tuple[frozenset[str] | None, bool, str | None], ...] | None:
    """A per-position check plan for a call whose argument-to-parameter
    assignment cannot depend on the argument types, or None when it can.

    This is the fast half of `validate_with_context`. The general path has to
    discover *which* parameter each argument landed on, and pays a symbol
    string, a regex match and a group walk per call to do it -- 36 times per
    evaluation of the benchmark expression, which is half its runtime. When
    every parameter consumes exactly one argument, none of that is needed:
    position `i` is parameter `i`, and all that is left is one type test per
    position that restricts anything.

    Decided the same way `direct_call_is_safe` decides its question, by
    exhausting the symbol alphabet rather than reasoning about the grammar.
    Combinations that match nothing are fine here -- they raise T0410 at run
    time, exactly as the general path would.
    """
    matcher = _compile_matcher(signature)
    if matcher is None or argc > len(matcher.params) or argc > 4:
        return None
    if any(p.regex.endswith("+") for p in matcher.params):
        return None
    for p in matcher.params[argc:]:
        # A trailing parameter this call does not supply must be genuinely
        # optional. One the *evaluation context* could fill instead shifts
        # the assignment, which is the case this plan cannot describe.
        if p.context or not p.regex.endswith("?"):
            return None
    want = [1] * argc + [0] * (len(matcher.params) - argc)
    for combo in product(_SYMBOLS, repeat=argc):
        m = matcher.regex.match("".join(combo))
        if m is None:
            continue  # a T0410 at run time, whichever path takes it
        if [len(m.group(i + 1)) for i in range(len(matcher.params))] != want:
            return None
    plan: list[tuple[frozenset[str] | None, bool, str | None]] = []
    for p in matcher.params:
        accepted = frozenset(sym for sym in _SYMBOLS if re.fullmatch(p.regex, sym))
        # A parameter that accepts every symbol needs no test at all.
        restrict = None if len(accepted) == len(_SYMBOLS) else accepted
        plan.append((restrict, p.array, p.subtype))
    return tuple(plan)


def _validate_fixed(
    plan: tuple[tuple[frozenset[str] | None, bool, str | None], ...],
    args: list[Any],
) -> list[Any]:
    # Two passes, because the reference matches the whole argument list
    # against the signature before it looks at any array's element types: a
    # T0410 on a later position outranks a T0412 on an earlier one, so
    # `$join(nums, nums)` is T0410.
    syms = [_fast_symbol(a) for a in args]
    for i, (accepted, _, _) in enumerate(plan[: len(args)]):
        if accepted is not None and syms[i] not in accepted:
            raise RuntimeEvaluationError(
                "T0410", f"Argument {i + 1} of function does not match function signature"
            )
    out: list[Any] = []
    for i, (_, is_array, subtype) in enumerate(plan[: len(args)]):
        out.append(_fixed_array_arg(args[i], syms[i], subtype, i) if is_array else args[i])
    return out


def _fixed_array_arg(arg: Any, sym: str, subtype: str | None, arg_idx: int) -> Any:
    """The array parameter rules, without the general path's group bookkeeping.

    The element scan is a class-membership test per element rather than a
    `_symbol` call, because this runs over the whole argument on the hot path
    (`$sum(items.value)` scans before it sums).
    """
    if sym == "m":
        return MISSING
    if subtype is not None:
        want = subtype[0]
        if sym != "a":
            if sym != want:
                _raise_t0412(subtype, arg_idx)
        elif arg:
            first = _fast_symbol(arg[0])
            if first != want or any(_fast_symbol(v) != first for v in arg):
                _raise_t0412(subtype, arg_idx)
    return arg if sym == "a" else [arg]


def _raise_t0412(subtype: str | None, arg_idx: int) -> None:
    raise RuntimeEvaluationError(
        "T0412",
        f"Argument {arg_idx + 1} of function must be an array of "
        f"{_ARRAY_TYPE_NAMES.get(subtype or '', 'values')}",
    )


def arg_check_specs(signature: str, argc: int) -> tuple[tuple[str, str] | None, ...] | None:
    """Per-argument checks a *fully supplied* call can apply inline.

    The translator asks this at compile time and wraps only the arguments
    that need it, then emits the built-in call directly. That is the whole
    difference between validation costing 31% of evaluation and costing
    nothing measurable: the wrapper form (`call_builtin_ctx` -> plan lookup
    -> validate -> splat a fresh list) is four Python calls per built-in
    call, and the benchmark makes 36 of them per evaluation.

    Returns None when the assignment is not fixed, so the caller falls back
    to the runtime path.
    """
    plan = _fixed_plan(signature, argc)
    if plan is None:
        return None
    # Inline checks run in argument order, but the reference matches the
    # *whole* argument list against the signature before it looks at any
    # array's element types -- so a T0410 on a later position outranks a
    # T0412 on an earlier one (`$join(nums, nums)` is T0410, not T0412).
    # Where both kinds are in play, hand the call back to the runtime path,
    # which sees every position before it decides. None of those signatures
    # is on a hot path.
    subtyped = sum(1 for _, is_array, sub in plan[:argc] if is_array and sub)
    restricted = sum(1 for acc, is_array, _ in plan[:argc] if acc is not None or is_array)
    if subtyped and restricted > 1:
        return None
    specs: list[tuple[str, str] | None] = []
    for accepted, is_array, subtype in plan[:argc]:
        if is_array:
            specs.append(("a", subtype or ""))
        elif accepted is not None:
            specs.append(("t", "".join(sorted(accepted))))
        else:
            specs.append(None)
    return tuple(specs)


def sig_t(value: Any, accepted: str, argno: int) -> Any:
    """One scalar-typed parameter position of a fully supplied call."""
    sym = _CLASS_SYMBOL.get(value.__class__)
    if (sym if sym is not None else _symbol(value)) not in accepted:
        raise RuntimeEvaluationError(
            "T0410", f"Argument {argno} of function does not match function signature"
        )
    return value


def sig_a(value: Any, subtype: str, argno: int) -> Any:
    """One array-typed parameter position: the element-type check and the
    reference's singleton-to-array promotion.

    Hand-inlined rather than delegating, because this is the shape almost
    every aggregate call takes (`$count(x)`, `$sum(x)`) and the delegation
    was two more Python calls per argument.
    """
    if value.__class__ is list:
        if not subtype:
            return value
        if value:
            want = subtype[0]
            first = _fast_symbol(value[0])
            if first != want or any(_fast_symbol(v) != first for v in value):
                _raise_t0412(subtype, argno - 1)
        return value
    if value is MISSING:
        return MISSING
    sym = _fast_symbol(value)
    if sym == "a":  # a list subclass, e.g. Preserved
        return sig_a(list(value), subtype, argno)
    if subtype and sym != subtype[0]:
        _raise_t0412(subtype, argno - 1)
    return [value]


def validate_with_context(signature: str, args: list[Any], context: Any) -> list[Any]:
    """Matches args against signature, substituting context for any focus
    ('-') parameter the arguments do not reach.

    Raises T0410 when the arguments do not fit the signature, T0411 when the
    context value is the wrong type to stand in for a parameter, and T0412
    when an array parameter's element types are wrong.
    """
    plan = _fixed_plan(signature, len(args))
    if plan is not None:
        return _validate_fixed(plan, args)

    matcher = _compile_matcher(signature)
    if matcher is None:
        return args

    supplied = "".join(_symbol(a) for a in args)
    m = matcher.regex.match(supplied)
    if m is None:
        _raise_t0410(matcher, args, supplied)

    validated: list[Any] = []
    arg_idx = 0
    for i, p in enumerate(matcher.params):
        group = m.group(i + 1)  # type: ignore[union-attr]
        if group == "":
            if p.context and p.context_regex is not None:
                if not p.context_regex.match(_symbol(context)):
                    raise RuntimeEvaluationError(
                        "T0411", f"Context value is not a compatible type with argument {arg_idx + 1}"
                    )
                validated.append(context)
            else:
                validated.append(args[arg_idx] if arg_idx < len(args) else MISSING)
                arg_idx += 1
            continue
        for single in group:
            arg = args[arg_idx] if arg_idx < len(args) else MISSING
            if p.type == "a":
                arg = _validate_array_arg(p, arg, single, group, arg_idx)
            validated.append(arg)
            arg_idx += 1
    return validated


def _validate_array_arg(p: _MatchParam, arg: Any, single: str, group: str, arg_idx: int) -> Any:
    """Applies an array parameter's element-type check and the reference's
    singleton-to-array promotion."""
    if single == "m":
        return MISSING
    ok = True
    if p.subtype is not None:
        if single != "a" and group != p.subtype:
            ok = False
        elif single == "a" and arg:
            item_type = _symbol(arg[0])
            ok = item_type == p.subtype[0] and all(_symbol(v) == item_type for v in arg)
    if not ok:
        raise RuntimeEvaluationError(
            "T0412",
            f"Argument {arg_idx + 1} of function must be an array of {_ARRAY_TYPE_NAMES.get(p.subtype or '', 'values')}",
        )
    return arg if single == "a" else [arg]


def call_builtin_ctx(fn: Any, signature: str, context: Any, args: list[Any]) -> Any:
    """Invokes a built-in whose arguments do not fill its signature, so the
    evaluation context may stand in for a focus ('-') parameter.

    The translator emits this only for those call sites; a call that supplies
    every parameter is compiled to a direct `fn_x(...)` call, which no amount
    of validation could change.
    """
    return fn(*validate_with_context(signature, args, context))


_SYMBOLS = ("a", "s", "n", "b", "l", "f", "o", "m")


@lru_cache(maxsize=256)
def direct_call_is_safe(signature: str, argc: int) -> bool:
    """True when passing argc arguments straight through, one per parameter,
    is what `validate_with_context` would have produced *whatever* those
    arguments turn out to be -- so the call needs no runtime validation.

    Decided by exhausting the symbol alphabet rather than by reasoning about
    it: an argument's type is one of eight symbols, and signatures are at
    most a handful of parameters wide, so the whole input space is small
    enough to check outright, once per (signature, arity) and cached. That
    keeps a hot call like `$string(x)` -- whose leading `x-` parameter
    accepts every symbol, and so can never be left for the context to fill
    -- on the direct call it has always compiled to.
    """
    matcher = _compile_matcher(signature)
    if matcher is None or argc > 4 or argc >= len(matcher.params):
        return False
    for i, p in enumerate(matcher.params):
        # Variadic or array params cannot be passed through unchanged.
        if p.array or p.regex.endswith("+"):
            return False
        if i < argc:
            continue
        # Parameter i is beyond the supplied arguments.
        if p.context:
            # A focus ('-') param here would receive the evaluation context,
            # a substitution the direct call cannot perform; it also skips
            # the T0411 type-check for that substitution.
            return False
        if not p.regex.endswith("?"):
            # Required parameter that will not be filled → T0410, not a
            # no-op pass-through.
            return False
    # Every supplied argument must land on the same parameter regardless of
    # its runtime type. Exhaust the symbol alphabet to confirm.
    want = [1] * argc + [0] * (len(matcher.params) - argc)
    for combo in product(_SYMBOLS, repeat=argc):
        m = matcher.regex.match("".join(combo))
        if m is None:
            return False  # some argument type would be an error
        if [len(m.group(i + 1)) for i in range(len(matcher.params))] != want:
            return False  # some argument type would shift the assignment
    return True
