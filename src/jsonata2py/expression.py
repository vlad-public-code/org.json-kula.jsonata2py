"""The public compiled-expression contract.

Ported from org.json_kula.jsonata_jvm.JsonataExpression /
AbstractJsonataExpression.

CompiledExpression wraps the module-level `_evaluate` function a
generated module defines (loader.py) and provides the stable public API:
evaluate, assign, register_function, use_library, set_timeout,
source_jsonata.

Concurrency (D9/§9): CompiledExpression and its evaluate() are
thread-safe. Generated modules hold only pure functions; per-instance
mutable state (permanent bindings, timeout) lives here,
guarded by a lock on writes -- reads are lock-free (dict reads are atomic
under the GIL, and safe under free-threaded builds since entries are only
ever replaced wholesale). Per-evaluation state lives in the ContextVar
(runtime/context.py, D5).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .bindings import JsonataBindings, JsonataBoundFunction, as_bound_function
from .errors import JsonataEvaluationError, _RuntimeEvaluationError
from .runtime import context as _ctx

if TYPE_CHECKING:
    from .library import JsonataLibrary


class CompiledExpression:
    def __init__(self, entry_point: Callable[..., Any], source_jsonata: str = "") -> None:
        self._entry_point = entry_point
        self._source_jsonata = source_jsonata
        self._values: dict[str, Any] = {}
        self._functions: dict[str, JsonataBoundFunction] = {}
        self._lock = threading.Lock()
        # Rebuilt lazily after assign()/register_function() rather than on
        # every evaluation -- merging per call dominated the cost of a
        # small expression.
        self._permanent: JsonataBindings | None = JsonataBindings()
        self._timeout_ms = 0
        self._eval_delegate: Callable[[str, Any], Any] | None = None

    @property
    def source_jsonata(self) -> str:
        return self._source_jsonata

    def set_timeout(self, timeout_ms: int) -> None:
        self._timeout_ms = timeout_ms

    def assign(self, name: str, value: Any) -> None:
        with self._lock:
            self._values[name] = value
            self._permanent = None

    def register_function(self, name: str, fn: Callable[..., Any] | JsonataBoundFunction) -> None:
        with self._lock:
            self._functions[name] = as_bound_function(fn)
            self._permanent = None

    def use_library(self, library: JsonataLibrary) -> None:
        for name, fn in library.functions.items():
            self.register_function(name, fn)
        for name, value in library.constants.items():
            self.assign(name, value)

    def _permanent_bindings(self) -> JsonataBindings:
        cached = self._permanent
        if cached is None:
            with self._lock:
                cached = self._permanent
                if cached is None:
                    cached = JsonataBindings()
                    for k, v in self._values.items():
                        cached.bind_value(k, v)
                    for k, fn in self._functions.items():
                        cached.bind_function(k, fn)
                    self._permanent = cached
        return cached

    def _set_eval_delegate(self, delegate: Callable[[str, Any], Any] | None) -> None:
        """Set by the factory that compiled this instance."""
        self._eval_delegate = delegate

    def evaluate(self, data: Any, bindings: JsonataBindings | None = None) -> Any:
        """Evaluates this JSONata expression against data, with no
        additional bindings beyond the permanent ones already registered,
        plus any per-evaluation bindings supplied.

        Returns the expression result, or values.MISSING if it yields no
        match -- JSONata's undefined. Raises JsonataEvaluationError if the
        expression cannot be applied to the given input.
        """
        _ctx.begin_evaluation(self._permanent_bindings(), bindings, self._timeout_ms, self._eval_delegate)
        try:
            return self._entry_point(data)
        except JsonataEvaluationError:
            raise
        except _RuntimeEvaluationError as e:
            raise JsonataEvaluationError(e.error_code, e.message, e) from e
        except RecursionError as e:
            raise JsonataEvaluationError(
                "U1001", "Stack overflow error: Check for circular reference or too many function calls", e
            ) from e
        except Exception as e:
            raise JsonataEvaluationError(None, str(e), e) from e
        finally:
            _ctx.end_evaluation()
