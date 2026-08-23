"""Call and composition support for JSONata function values.

Ported from org.json_kula.jsonata_jvm.runtime.LambdaRegistry and
BoundFunctionValue.java.

A function value *is* a JLambda (D6) -- it carries its callable directly,
so calling it is an attribute access rather than a registry lookup, and it
stays callable for exactly as long as something references it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..errors import _RuntimeEvaluationError as RuntimeEvaluationError
from . import context as _ctx
from .values import MISSING, UNKNOWN_ARITY, JLambda

if TYPE_CHECKING:
    from .signature import BoundFunction

# Maximum nesting depth for user-defined function calls (JSONata U1001 limit).
MAX_CALL_DEPTH = 100

# Maximum number of trampoline iterations for TCO'd tail-recursive loops.
MAX_TRAMPOLINE_ITERATIONS = 100_000


class _TcoSentinel:
    """Sentinel returned by fn_apply_tco to signal a pending tail call. A
    dedicated object rather than a magic string (cleaner than the Java
    TextNode hack, and impossible to collide with data)."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "TCO_SENTINEL"


TCO_SENTINEL = _TcoSentinel()


@dataclass(frozen=True, slots=True)
class TailCallData:
    """Carries the next tail-call target (lambda + arg) when TCO_SENTINEL is
    returned."""

    fn: Callable[[Any], Any]
    arg: Any


# Fallback call-depth counter / pending-tail-call slot used when fn_apply is
# called outside an active evaluation (e.g. in tests). Under normal
# evaluation these live on EvalState instead.
_FALLBACK_CALL_DEPTH: list[int] = [0]
_FALLBACK_PENDING_TAIL_CALL: list[TailCallData | None] = [None]


def lambda_node(fn: Callable[[Any], Any], arity: int = UNKNOWN_ARITY) -> JLambda:
    """Wraps fn as a JSONata function value."""
    return JLambda(fn, arity)


def is_lambda_token(n: Any) -> bool:
    return isinstance(n, JLambda)


def lookup_lambda(n: Any) -> Callable[[Any], Any]:
    if not isinstance(n, JLambda):
        raise RuntimeEvaluationError("T1006", f"The expression is not a function; got: {n}")
    return n.fn


def arity_of(n: Any) -> int:
    return n.arity if isinstance(n, JLambda) else UNKNOWN_ARITY


def lambda_arity(n: Any) -> int:
    return arity_of(n)


def fn_pipe(arg: Any, fn: Any) -> Any:
    """Implements the ~> (chain/pipe) operator."""
    from . import core as _core
    from .strings import regex_ops as _regex_ops
    from .values import is_regex

    if is_regex(fn):
        if arg is MISSING:
            return MISSING
        return _regex_ops.test_match(fn, _core.to_text(arg))
    if not is_lambda_token(fn):
        raise RuntimeEvaluationError("T2006", f"Right-hand side of ~> is not a function; got: {fn}")
    if is_lambda_token(arg):
        f = lookup_lambda(arg)
        g = lookup_lambda(fn)
        return lambda_node(lambda x: g(f(x)), arity_of(arg))
    return lookup_lambda(fn)(arg)


def fn_apply(fn: Any, arg: Any) -> Any:
    """Applies fn to arg -- used when calling a user-defined lambda stored
    in a local variable.

    Implements a trampoline for tail-call optimisation (TCO): a tail call
    returns TCO_SENTINEL and is re-dispatched here instead of growing the
    Python call stack.
    """
    if isinstance(fn, JLambda):
        eval_state = _ctx.get_state()
        depth = eval_state.get_call_depth() if eval_state is not None else _FALLBACK_CALL_DEPTH
        pending_slot = eval_state.get_pending_tail_call() if eval_state is not None else None

        if depth[0] >= MAX_CALL_DEPTH:
            raise RuntimeEvaluationError(
                "U1001", "Stack overflow error: Check for circular reference or too many function calls"
            )
        if eval_state is not None and eval_state.timeout_deadline is not None:
            import time

            if time.monotonic() > eval_state.timeout_deadline:
                raise RuntimeEvaluationError("U1001", "Expression evaluation timeout")
        depth[0] += 1
        try:
            try:
                result = fn.fn(arg)
            except RecursionError:
                raise RuntimeEvaluationError(
                    "U1001", "Stack overflow error: Check for circular reference or too many function calls"
                ) from None
            trampoline_count = 0
            while result is TCO_SENTINEL:
                trampoline_count += 1
                if trampoline_count > MAX_TRAMPOLINE_ITERATIONS:
                    raise RuntimeEvaluationError(
                        "U1001", "Stack overflow error: Check for circular reference or too many function calls"
                    )
                if pending_slot is not None:
                    tcd = pending_slot[0]
                    pending_slot[0] = None
                else:
                    tcd = _FALLBACK_PENDING_TAIL_CALL[0]
                    _FALLBACK_PENDING_TAIL_CALL[0] = None
                if tcd is None:
                    raise RuntimeEvaluationError(
                        "U1001", "Stack overflow error: Check for circular reference or too many function calls"
                    )
                result = tcd.fn(tcd.arg)
            return result
        finally:
            depth[0] -= 1
            if pending_slot is not None:
                pending_slot[0] = None
            else:
                _FALLBACK_PENDING_TAIL_CALL[0] = None
    raise RuntimeEvaluationError("T1006", f"The expression is not a function; got: {fn}")


def fn_apply_tco(fn: Any, arg: Any) -> Any:
    """Tail-call variant of fn_apply: stores the next call as a pending
    tail call and returns TCO_SENTINEL. Must only be called from strict
    tail position in a lambda body."""
    if not isinstance(fn, JLambda):
        raise RuntimeEvaluationError("T1006", f"The expression is not a function; got: {fn}")
    tcd = TailCallData(fn.fn, arg)
    eval_state = _ctx.get_state()
    if eval_state is not None:
        eval_state.get_pending_tail_call()[0] = tcd
    else:
        _FALLBACK_PENDING_TAIL_CALL[0] = tcd
    return TCO_SENTINEL


# =============================================================================
# Bound-function adaptation (was BoundFunctionValue.java)
# =============================================================================
#
# Adapts a bound function (Phase 6, bindings.py) to a JSONata function
# *value* -- a JLambda that can be stored in a variable, passed to $map, or
# piped through ~>. This is the mirror image of the library export
# adapter, which adapts a function value to the bound-function contract.


def bound_function_value(name: str, fn: BoundFunction) -> JLambda:
    """Wraps fn as a function value."""
    from .signature import arity_of as sig_arity_of

    arity = sig_arity_of(fn.get_function_signature())
    return JLambda(lambda arg: _invoke_bound(name, fn, arity, arg), arity)


def _invoke_bound(name: str, fn: BoundFunction, arity: int, arg: Any) -> Any:
    """Unpacks arg per the declared arity and calls fn. None (JSON null,
    D1) is a real argument value at either an in-bounds packed-array slot
    or as the whole single argument -- only an out-of-range slot becomes
    MISSING."""
    if arity == 0:
        args: list[Any] = []
    elif arity >= 2 and isinstance(arg, list):
        args = [arg[i] if i < len(arg) else MISSING for i in range(arity)]
    else:
        args = [arg]
    return call_bound_function_value(name, fn, args)


def call_bound_function_value(name: str, fn: BoundFunction, args: list[Any]) -> Any:
    """Coerces args against the function's signature and calls it -- the
    same sequence context.call_bound_function applies to a direct
    $name(...) call, so a bound function behaves identically whether
    called by name or through a value."""
    from ..bindings import JsonataFunctionArguments
    from ..errors import JsonataEvaluationError
    from .signature import coerce as sig_coerce

    coerced = sig_coerce(fn.get_function_signature(), args)
    try:
        return fn.apply(JsonataFunctionArguments(coerced))
    except JsonataEvaluationError as e:
        reason = e.message
        suffix = f": {reason}" if reason and reason.strip() else ""
        raise RuntimeEvaluationError(e.error_code, f"Error calling bound function ${name}{suffix}") from e


def apply_bound_function_value(fn_value: Any, args: list[Any]) -> Any:
    """Applies a function value bound under a name to a call site's
    argument list, packing them the way generated code packs a call to a
    local function variable."""
    if not args:
        arg: Any = None
    elif len(args) == 1:
        arg = args[0]
    else:
        from . import core as _core

        arg = _core.pack_args(*args)
    return fn_apply(fn_value, arg)
