"""A JSONata *library*: a definition expression, and everything it
exports.

Ported from org.json_kula.jsonata_jvm.JsonataLibrary /
JsonataLibraryOptions / ExportedJsonataFunction.

A definition expression is ordinary JSONata. It binds names -- functions,
values, or both -- and returns the names to export as an array of
strings. It evaluates on its own in any JSONata engine, where its result
is simply that list:

    (
      $pi := 3.1415926535897932384626;
      $product := function($a, $b) { $a * $b };
      $factorial := function($n) { $n = 0 ? 1 : $reduce([1..$n], $product) };
      $sin := function($x){ $cos($x - $pi/2) };
      $cos := function($x){ ... };

      ["sin", "cos", "pi"]
    )

    lib = factory.compile_library(definition)
    report = factory.compile("angles.$sin($) * $pi")
    report.use_library(lib)   # or the two dicts by hand:
    for name, fn in lib.functions.items():
        report.register_function(name, fn)
    for name, value in lib.constants.items():
        report.assign(name, value)

Each exported name lands in .functions or .constants according to what it
evaluated to -- the definition does not have to say which is which. Names
it binds but does not export ($product, $factorial) stay internal, while
remaining reachable from the exported closures; mutual recursion between
exported functions works.

Semantics worth knowing:
  * The definition is self-contained. Every name it uses must be one it
    binds, a JSONata built-in, or one supplied through
    JsonataLibraryOptions.bindings -- anything else is rejected when the
    library is compiled, rather than resolved against whatever happens to
    be bound where an exported function is called.
  * The caller's evaluation is reused. Called from inside an expression,
    an exported function shares that evaluation's recursion budget (100
    nested calls) and timeout.
  * Thread-safe. The definition runs exactly once, at build time;
    afterwards the closure graph is read-only and the exported functions
    may be called concurrently.
  * Functions returned at call time are not durable. If an exported
    function returns a *new* function, that one lives only for the
    evaluation that produced it -- the same rule as any lambda created
    mid-expression.

LambdaScope (Java's durable scope for minted function values) is not
needed here: a Python exported function stays callable exactly as long as
something references it, which is the whole point of JLambda holding its
callable directly (D6).
"""

from __future__ import annotations

from typing import Any

from . import _export_rewriter as _rewriter
from .bindings import JsonataBindings, JsonataBoundFunction
from .errors import JsonataEvaluationError, _RuntimeEvaluationError


class JsonataLibraryOptions:
    """Optional settings for JsonataExpressionFactory.compile_library.

    All settings have sensible defaults; a definition expression that
    only binds lambdas needs none of them.

        lib = factory.compile_library(
            definition,
            JsonataLibraryOptions()
            .with_bindings(JsonataBindings().bind_value("vatRate", rate))
            .with_signature("netOf", "<n:n>"),
        )
    """

    __slots__ = ("_bindings", "_input", "_signatures")

    def __init__(self) -> None:
        self._input: Any = None
        self._bindings: JsonataBindings | None = None
        self._signatures: dict[str, str] = {}

    def with_input(self, input_: Any) -> JsonataLibraryOptions:
        """Sets the document the definition expression is evaluated
        against. Defaults to JSON null; only needed when the definition
        reads from the input to build its functions."""
        self._input = input_
        return self

    def with_bindings(self, bindings: JsonataBindings | None) -> JsonataLibraryOptions:
        """Sets bindings the definition may rely on: the way to
        parameterise a library.

        A definition must be self-contained, so a name it neither binds
        nor gets from the JSONata standard library has to be supplied
        here -- otherwise compiling the library fails. These names are in
        scope while the definition runs, are captured by the functions it
        exports, and are installed again when an exported function is
        called directly from Python, outside any evaluation."""
        self._bindings = bindings
        return self

    def with_signature(self, function_name: str, signature: str) -> JsonataLibraryOptions:
        """Overrides the reported signature of one exported function,
        tightening argument validation and coercion at the Python
        boundary (e.g. "<n:n>" to coerce a numeric string to a number).
        The leading $ on the name is optional.

        Without an override, a function declaring its own signature in
        JSONata (function($x)<n:n>{...}) reports that signature, and any
        other function reports an all-optional <j?...:j> -- the same
        permissiveness a JSONata call site has."""
        self._signatures[_rewriter.normalize(function_name)] = signature
        return self

    @property
    def input(self) -> Any:
        return self._input

    @property
    def bindings(self) -> JsonataBindings | None:
        return self._bindings

    def signature_override(self, normalized_name: str) -> str | None:
        return self._signatures.get(normalized_name)


class JsonataLibrary:
    """A compiled library: holds the exported functions and constants in
    export-list order. Construct via
    JsonataExpressionFactory.compile_library, not directly."""

    def __init__(self, source_jsonata: str, definition: Any, definition_bindings: JsonataBindings | None) -> None:
        self._source_jsonata = source_jsonata
        self._definition = definition
        self._definition_bindings = definition_bindings
        self._closed = False
        self._functions: dict[str, JsonataBoundFunction] = {}
        self._constants: dict[str, Any] = {}

    def _export_function(self, name: str, fn: JsonataBoundFunction) -> None:
        self._functions[name] = fn

    def _export_constant(self, name: str, value: Any) -> None:
        self._constants[name] = value

    @property
    def functions(self) -> dict[str, JsonataBoundFunction]:
        """The exported functions, keyed by name without the leading $,
        in the order the definition listed them. Ready for
        JsonataBindings.bind_function / bind_functions or
        CompiledExpression.register_function -- though use_library takes
        this and .constants together."""
        return dict(self._functions)

    @property
    def constants(self) -> dict[str, Any]:
        """The exported values -- every exported name that did not
        evaluate to a function -- keyed the same way. Ready for
        JsonataBindings.bind_value or CompiledExpression.assign.

        They are values, not expressions: the definition ran once, at
        compile time, so a constant is whatever it evaluated to then."""
        return dict(self._constants)

    @property
    def source_jsonata(self) -> str:
        return self._source_jsonata

    @property
    def is_open(self) -> bool:
        """True until close() is called."""
        return not self._closed

    def close(self) -> None:
        """Retires the exported functions: calling one afterwards raises
        JsonataEvaluationError. Constants already handed out keep
        working -- they are ordinary values. Idempotent, and never
        required: a library that simply becomes unreachable is collected
        like any other object."""
        self._closed = True

    def __enter__(self) -> JsonataLibrary:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _check_open(self, function_name: str) -> None:
        if self._closed:
            raise JsonataEvaluationError(None, f"Error calling exported function ${function_name}: its library has been closed")

    def _begin_standalone_frame(self) -> None:
        """Opens an evaluation frame for a call made from plain Python,
        outside any expression. The frame carries the library's
        definition-time bindings so that a function whose body references
        a value the definition did not bind still resolves it."""
        from .runtime import context as _ctx

        _ctx.begin_evaluation(self._definition._permanent_bindings(), self._definition_bindings, 0)

    def __repr__(self) -> str:
        return f"JsonataLibrary[functions={list(self._functions)}, constants={list(self._constants)}]"


class _ExportedJsonataFunction:
    """Adapts one lambda exported from a JsonataLibrary to the
    JsonataBoundFunction contract.

    Calls are forwarded to the runtime's fn_apply, which resolves the
    lambda token, runs the tail-call trampoline, and enforces the
    recursion limit -- exactly as a call from inside a JSONata expression
    would.
    """

    __slots__ = ("_arity", "_name", "_owner", "_signature", "_token")

    def __init__(self, name: str, token: Any, signature: str | None, arity: int, owner: JsonataLibrary) -> None:
        self._name = name
        self._token = token
        self._signature = signature
        self._arity = arity
        self._owner = owner

    def get_function_signature(self) -> str | None:
        return self._signature

    def apply(self, args: Any) -> Any:
        from .runtime import context as _ctx
        from .runtime.lambdas import fn_apply

        self._owner._check_open(self._name)
        arg = self._pack_arguments(args)

        # Inside a caller's evaluation we deliberately reuse its frame, so
        # the function observes the caller's bindings, recursion budget
        # and timeout. Called directly from Python there is no frame at
        # all, and runtime helpers ($millis, regex caches, $-bindings)
        # need one.
        own_frame = _ctx.get_state() is None
        if own_frame:
            self._owner._begin_standalone_frame()
        try:
            return fn_apply(self._token, arg)
        except _RuntimeEvaluationError as e:
            raise JsonataEvaluationError(e.error_code, f"Error calling exported function ${self._name}: {e.message}", e) from e
        finally:
            if own_frame:
                _ctx.end_evaluation()

    def _pack_arguments(self, args: Any) -> Any:
        """Applies the runtime's calling convention for user-defined
        functions: no argument becomes None (D1: MISSING, not JSON null --
        the callee sees "no argument supplied"), one argument is passed
        through, and several are packed into a non-flattening tuple that
        the lambda body unpacks positionally.

        The declared arity decides, not len(args) -- the signature
        machinery pads a short argument list with MISSING to the declared
        length, and the generated unpack code treats a missing slot as an
        absent parameter. When the arity is unknown (-1 -- the binding's
        value was computed rather than a literal lambda) the supplied
        count decides."""
        from .runtime import core as _core
        from .runtime.values import MISSING

        effective_arity = self._arity if self._arity >= 0 else len(args)
        if effective_arity == 0:
            return None
        if effective_arity == 1:
            return MISSING if len(args) == 0 else args.get(0)
        return _core.pack_args(*args.as_list())

    def __repr__(self) -> str:
        sig = self._signature or ""
        return f"${self._name}{sig} from {self._owner!r}"
