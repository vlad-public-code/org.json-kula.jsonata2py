"""Per-evaluation bindings/timeout/regex-cache context for active JSONata
evaluations.

Ported from org.json_kula.jsonata_jvm.runtime.EvaluationContext (D5).

Java uses a ThreadLocal<EvalState>. Python uses a contextvars.ContextVar so
that both plain threads AND asyncio tasks get correct isolation: each
asyncio task runs in its own copied context, so an await-interleaved
evaluation cannot pick up another task's bindings. A new thread starts with
an empty context, so an expression instance shared across threads behaves
like the Java one: permanent bindings live on the CompiledExpression
instance, not in the context.

Timeout: time.monotonic() is used for the deadline (immune to clock steps);
time.time()-derived epoch milliseconds are used for $now/$millis, which are
observable values and must be wall-clock.

Bindings are duck-typed here (not imported from bindings.py) to avoid a
runtime <-> public-API circular import: anything with is_empty(),
get_values(), get_functions(), get_value(name), get_function(name) works.
JsonataBindings (Phase 6, bindings.py) implements this shape.
"""

from __future__ import annotations

import time
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Protocol

from ..errors import _RuntimeEvaluationError as RuntimeEvaluationError
from .values import MISSING

if TYPE_CHECKING:
    from .lambdas import TailCallData


class Bindings(Protocol):
    def is_empty(self) -> bool: ...
    def get_values(self) -> dict[str, Any]: ...
    def get_functions(self) -> dict[str, Any]: ...
    def get_value(self, name: str) -> Any:
        """Returns the bound value, or MISSING when name is not bound.
        MISSING -- not None -- is the not-found sentinel: None is JSON null,
        which is a real bindable value (D1)."""
        ...

    def get_function(self, name: str) -> Any | None: ...


class _MergedBindings:
    """A simple dict-backed Bindings used only to merge permanent + per-eval
    bindings when both are non-empty (mirrors the Java merge branch)."""

    __slots__ = ("_functions", "_values")

    def __init__(self, values: dict[str, Any], functions: dict[str, Any]) -> None:
        self._values = values
        self._functions = functions

    def is_empty(self) -> bool:
        return not self._values and not self._functions

    def get_values(self) -> dict[str, Any]:
        return self._values

    def get_functions(self) -> dict[str, Any]:
        return self._functions

    def get_value(self, name: str) -> Any:
        return self._values.get(name, MISSING)

    def get_function(self, name: str) -> Any | None:
        return self._functions.get(name)


_EMPTY_BINDINGS = _MergedBindings({}, {})


class _Frame:
    """One suspended evaluation. Created only when an evaluation starts
    inside another ($eval, or a bound function that evaluates an expression
    of its own) -- without this the inner evaluation would overwrite the
    outer one and clear it on the way out."""

    __slots__ = (
        "bindings",
        "call_depth",
        "eval_delegate",
        "millis",
        "pending_tail_call",
        "suspended",
        "timeout_deadline",
    )

    def __init__(
        self,
        bindings: Bindings,
        millis: int,
        timeout_deadline: float | None,
        eval_delegate: Any,
        call_depth: list[int] | None,
        pending_tail_call: list[TailCallData | None] | None,
        suspended: _Frame | None,
    ) -> None:
        self.bindings = bindings
        self.millis = millis
        self.timeout_deadline = timeout_deadline
        self.eval_delegate = eval_delegate
        self.call_depth = call_depth
        self.pending_tail_call = pending_tail_call
        self.suspended = suspended


# A process-wide stack of the deadlines currently installed by *any*
# thread or task, mirroring jsonata2js's `lambda.js#_deadlineStack`. It
# exists purely as a cheap negative gate: reading `if not
# DEADLINE_STACK:` is one global load plus one truthiness test, where
# the authoritative per-context answer (`has_deadline()`) costs a
# function call plus a ContextVar lookup plus two attribute loads --
# ~160 ns paid on every callback-taking runtime helper even when no
# caller has ever set a timeout.
#
# Non-empty never *asserts* that this context has a deadline, so a
# concurrent timed evaluation in another thread only costs untimed
# evaluations the slow path they used to take unconditionally; empty is
# authoritative because a deadline is pushed before any generated code
# can observe it and popped in EvalState.end's finally-driven caller.
# list.append/pop are atomic, and every push is matched by exactly one
# pop, so the length never drifts.
DEADLINE_STACK: list[float] = []


class EvalState:
    """All per-evaluation context-local state in one mutable container.
    Reused across evaluate() calls within the same context to avoid
    per-call allocation. call_depth / pending_tail_call are lazily
    initialised on first access (only needed once user-defined functions
    are called)."""

    __slots__ = (
        "active",
        "bindings",
        "call_depth",
        "eval_delegate",
        "millis",
        "pending_tail_call",
        "suspended",
        "timeout_deadline",
    )

    def __init__(self) -> None:
        self.active = False
        self.bindings: Bindings | None = None
        self.millis = 0
        self.timeout_deadline: float | None = None
        self.call_depth: list[int] | None = None
        self.pending_tail_call: list[TailCallData | None] | None = None
        self.eval_delegate: Any = None
        self.suspended: _Frame | None = None

    def begin(
        self,
        bindings: Bindings,
        millis: int,
        timeout_ms: int,
        eval_delegate: Any,
    ) -> None:
        nested = self.active
        if nested:
            # Nested evaluation: suspend the enclosing one rather than
            # overwriting it. The inner evaluation gets its own recursion
            # budget, restored on the way out.
            self.suspended = _Frame(
                self.bindings,  # type: ignore[arg-type]
                self.millis,
                self.timeout_deadline,
                self.eval_delegate,
                self.call_depth,
                self.pending_tail_call,
                self.suspended,
            )
            self.call_depth = None
            self.pending_tail_call = None
        self.active = True
        self.bindings = bindings
        # $now/$millis are frozen for the whole top-level evaluation, and
        # a nested $eval is part of that same evaluation -- so it INHERITS
        # the outer snapshot instead of taking a fresh one. Otherwise
        # `$eval("$millis()") = $millis()` could be false, which the
        # reference guarantees true (jsonata2js keeps the same invariant
        # by having $eval reuse clock.js's pushed snapshot rather than
        # calling createClock itself).
        if not nested:
            self.millis = millis
        deadline = time.monotonic() + timeout_ms / 1000.0 if timeout_ms > 0 else None
        self.timeout_deadline = deadline
        if deadline is not None:
            DEADLINE_STACK.append(deadline)
        self.eval_delegate = eval_delegate

    def end(self) -> None:
        if self.timeout_deadline is not None:
            DEADLINE_STACK.pop()
        outer = self.suspended
        if outer is not None:
            self.suspended = outer.suspended
            self.bindings = outer.bindings
            self.millis = outer.millis
            self.timeout_deadline = outer.timeout_deadline
            self.eval_delegate = outer.eval_delegate
            self.call_depth = outer.call_depth
            self.pending_tail_call = outer.pending_tail_call
            return
        self.active = False
        self.bindings = None
        self.eval_delegate = None
        # Cleared so a stray second end() cannot pop a deadline this
        # state no longer owns; has_deadline() gates on `active` anyway.
        self.timeout_deadline = None
        if self.call_depth is not None:
            self.call_depth[0] = 0
        if self.pending_tail_call is not None:
            self.pending_tail_call[0] = None

    def get_call_depth(self) -> list[int]:
        if self.call_depth is None:
            self.call_depth = [0]
        return self.call_depth

    def get_pending_tail_call(self) -> list[TailCallData | None]:
        if self.pending_tail_call is None:
            self.pending_tail_call = [None]
        return self.pending_tail_call


_CURRENT: ContextVar[EvalState | None] = ContextVar("jsonata_eval_state", default=None)


def _current() -> EvalState:
    s = _CURRENT.get()
    if s is None:
        s = EvalState()
        _CURRENT.set(s)
    return s


def begin_evaluation(
    permanent: Bindings,
    per_eval: Bindings | None,
    timeout_ms: int,
    eval_delegate: Any = None,
) -> None:
    """Installs the bindings visible to this evaluation: the expression's
    permanent set, overlaid with any per-evaluation set. Must be paired
    with end_evaluation() in a finally block."""
    if per_eval is None or per_eval.is_empty():
        merged = permanent
    elif permanent.is_empty():
        merged = per_eval
    else:
        values = dict(permanent.get_values())
        functions = dict(permanent.get_functions())
        values.update(per_eval.get_values())
        functions.update(per_eval.get_functions())
        merged = _MergedBindings(values, functions)
    _current().begin(merged, int(time.time() * 1000), timeout_ms, eval_delegate)


def get_eval_delegate() -> Any:
    s = _current()
    return s.eval_delegate if s.active else None


def empty_bindings() -> Bindings:
    return _EMPTY_BINDINGS


def is_active() -> bool:
    return _current().active


def has_deadline() -> bool:
    if not DEADLINE_STACK:
        return False
    s = _current()
    return s.active and s.timeout_deadline is not None


def check_timeout() -> None:
    """Raises U1001 if the current evaluation has exceeded its deadline.
    No-op when no timeout is set."""
    if not DEADLINE_STACK:
        return
    s = _current()
    if s.active and s.timeout_deadline is not None and time.monotonic() > s.timeout_deadline:
        raise RuntimeEvaluationError("U1001", "Expression evaluation timeout")


def end_evaluation() -> None:
    _current().end()


def get_state() -> EvalState | None:
    """Returns the full eval state for the current context, or None if
    outside an evaluation."""
    s = _current()
    return s if s.active else None


def evaluation_millis() -> int:
    """Returns the evaluation-start timestamp in epoch milliseconds. Falls
    back to the current wall-clock time if called outside an active
    evaluation."""
    s = _current()
    return s.millis if s.active else int(time.time() * 1000)


def resolve_binding(name: str) -> Any:
    """Resolves a named value from the active bindings. A name bound as a
    function resolves to a function value (see lambdas.bound_function_value)
    so it can be passed to $map, piped through ~>, etc."""
    from . import lambdas  # local import: breaks context<->lambdas cycle

    s = _current()
    if not s.active:
        return MISSING
    assert s.bindings is not None
    v = s.bindings.get_value(name)
    if v is not MISSING:
        return v
    fn = s.bindings.get_function(name)
    if fn is not None:
        return lambdas.bound_function_value(name, fn)
    return MISSING


def call_bound_function(name: str, args: list[Any]) -> Any:
    """Calls a named function from the active bindings. Falls back to the
    values map: a value that is a function is callable as $name(...) too."""
    from . import lambdas  # local import: breaks context<->lambdas cycle

    s = _current()
    if s.active:
        assert s.bindings is not None
        fn = s.bindings.get_function(name)
        if fn is not None:
            return lambdas.call_bound_function_value(name, fn, args)
        value = s.bindings.get_value(name)
        if lambdas.is_lambda_token(value):
            return lambdas.apply_bound_function_value(value, args)
    raise RuntimeEvaluationError("T1006", f"The function '{name}' is not defined")
