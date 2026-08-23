"""Named values and functions injected into a JSONata expression at
evaluation time.

Ported from org.json_kula.jsonata_jvm.JsonataBindings /
JsonataBoundFunction / JsonataFunctionArguments.

Within a JSONata expression, bound values are referenced as $name and
bound functions are called as $name(args...). A bound function is also
usable as a value, and a bound value that is a function is callable.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from .runtime.values import MISSING

if TYPE_CHECKING:
    from .library import JsonataLibrary


@runtime_checkable
class JsonataBoundFunction(Protocol):
    """A Python callable that can be bound into a JSONata expression via
    CompiledExpression.register_function or JsonataBindings.

    See runtime/signature.py for the <params:return> signature mini-language.
    """

    def get_function_signature(self) -> str | None: ...

    def apply(self, args: JsonataFunctionArguments) -> Any: ...


class JsonataFunctionArguments:
    """The argument list passed to a JsonataBoundFunction. Accessing an
    index beyond the actual argument count returns MISSING rather than
    raising, consistent with JSONata's "optional argument" semantics."""

    __slots__ = ("_args",)

    def __init__(self, args: list[Any]) -> None:
        self._args = list(args)

    def get(self, index: int) -> Any:
        if index < 0 or index >= len(self._args):
            return MISSING
        return self._args[index]

    def __len__(self) -> int:
        return len(self._args)

    def as_list(self) -> list[Any]:
        return list(self._args)


def bound_function(
    signature: str | None = None,
) -> Callable[[Callable[..., Any]], JsonataBoundFunction]:
    """Decorator adapting a plain Python callable to the JsonataBoundFunction
    contract.

    @jsonata.bound_function("<n:n>")
    def round2(n: float) -> float:
        return round(n, 2)
    """

    def decorate(fn: Callable[..., Any]) -> JsonataBoundFunction:
        return _DecoratedFunction(fn, signature)

    return decorate


class _DecoratedFunction:
    """Adapts a plain callable (optionally with a declared signature) to
    JsonataBoundFunction. A bare callable with no signature gets arity-1
    semantics as a function value, matching the rule Java applies when a
    signature doesn't pin the arity down."""

    __slots__ = ("_fn", "_signature")

    def __init__(self, fn: Callable[..., Any], signature: str | None) -> None:
        self._fn = fn
        self._signature = signature

    def get_function_signature(self) -> str | None:
        return self._signature

    def apply(self, args: JsonataFunctionArguments) -> Any:
        return self._fn(*args.as_list())


def as_bound_function(fn: Callable[..., Any] | JsonataBoundFunction) -> JsonataBoundFunction:
    """Accepts either a JsonataBoundFunction implementation or a plain
    callable and returns a JsonataBoundFunction."""
    if isinstance(fn, JsonataBoundFunction):
        return fn
    return _DecoratedFunction(fn, None)


class JsonataBindings:
    """A set of named values and named functions to inject into a JSONata
    expression.

    b = (
        JsonataBindings()
        .bind_value("taxRate", 0.2)
        .bind_function("round2", round2)
    )
    result = expr.evaluate(data, b)
    """

    __slots__ = ("_functions", "_values")

    def __init__(self) -> None:
        self._values: dict[str, Any] = {}
        self._functions: dict[str, JsonataBoundFunction] = {}

    def bind_value(self, name: str, value: Any) -> JsonataBindings:
        self._values[name] = value
        return self

    def bind_function(self, name: str, fn: Callable[..., Any] | JsonataBoundFunction) -> JsonataBindings:
        self._functions[name] = as_bound_function(fn)
        return self

    def bind_functions(self, fns: dict[str, Callable[..., Any] | JsonataBoundFunction]) -> JsonataBindings:
        for name, fn in fns.items():
            self._functions[name] = as_bound_function(fn)
        return self

    def use_library(self, library: JsonataLibrary) -> JsonataBindings:
        """Binds everything library exports: its functions as function
        bindings, its constants as value bindings."""
        for name, fn in library.functions.items():
            self._functions[name] = as_bound_function(fn)
        self._values.update(library.constants)
        return self

    def get_value(self, name: str) -> Any:
        """Returns the bound value, or MISSING when name is not bound. A value
        bound as JSON null returns None, which is a real value and must not be
        conflated with "not bound" (D1)."""
        return self._values.get(name, MISSING)

    def get_function(self, name: str) -> JsonataBoundFunction | None:
        return self._functions.get(name)

    def is_empty(self) -> bool:
        return not self._values and not self._functions

    def get_values(self) -> dict[str, Any]:
        return dict(self._values)

    def get_functions(self) -> dict[str, JsonataBoundFunction]:
        return dict(self._functions)
