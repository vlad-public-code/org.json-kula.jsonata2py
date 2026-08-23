"""jsonata2py -- a JSONata-to-Python compiler.

Parses a JSONata expression once, optimises the AST, translates it to
Python source, compiles it with the host compiler, and returns a
reusable callable. Ported from jsonata-jvm-compiler (Java).

    import jsonata2py as jsonata
    expr = jsonata.compile("Account.Order.Product.Price * 1.2")
    result = expr.evaluate(data)
"""

from __future__ import annotations

import threading
from collections.abc import Sequence

from .bindings import JsonataBindings, JsonataBoundFunction, JsonataFunctionArguments, bound_function
from .errors import JsonataCompilationError, JsonataError, JsonataEvaluationError, ParseError
from .expression import CompiledExpression
from .factory import JsonataExpressionFactory
from .library import JsonataLibrary, JsonataLibraryOptions
from .runtime.values import MISSING

__version__ = "0.1.0"

__all__ = [
    "MISSING",
    "CompiledExpression",
    "JsonataBindings",
    "JsonataBoundFunction",
    "JsonataCompilationError",
    "JsonataError",
    "JsonataEvaluationError",
    "JsonataExpressionFactory",
    "JsonataFunctionArguments",
    "JsonataLibrary",
    "JsonataLibraryOptions",
    "ParseError",
    "bound_function",
    "compile",
    "compile_all",
]

_default_factory: JsonataExpressionFactory | None = None
# Without this, two threads racing the first compile() each build their own
# factory and one is discarded -- splitting the compile caches, so the
# survivor's cache misses on everything the loser had already compiled.
_default_factory_lock = threading.Lock()


def _factory() -> JsonataExpressionFactory:
    global _default_factory
    factory = _default_factory
    if factory is None:
        with _default_factory_lock:
            factory = _default_factory
            if factory is None:
                factory = _default_factory = JsonataExpressionFactory()
    return factory


def compile(expression: str) -> CompiledExpression:
    """Compiles expression using a lazily-created process-wide default
    factory. For repeated compilation in a hot path, prefer constructing
    your own JsonataExpressionFactory and reusing it."""
    return _factory().compile(expression)


def compile_all(expressions: Sequence[str]) -> list[CompiledExpression]:
    return _factory().compile_all(expressions)
