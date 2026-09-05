"""Converts a JSONata expression string into a compiled, ready-to-evaluate
CompiledExpression.

Ported from org.json_kula.jsonata_jvm.JsonataExpressionFactory.

Pipeline: Parser.parse -> Optimizer.optimize -> Translator.translate ->
ExpressionLoader.load -> CompiledExpression.

    factory = JsonataExpressionFactory()
    expr = factory.compile("Account.Order.Product.Price * 1.2")
    result = expr.evaluate(data)

Instances are thread-safe and may be reused for many compile() calls.
"""

from __future__ import annotations

import itertools
import threading
import weakref
from collections import OrderedDict
from collections.abc import Sequence
from typing import Any

from . import _export_rewriter
from .errors import JsonataCompilationError, JsonataEvaluationError, LoadError, ParseError, TranslatorError
from .expression import CompiledExpression
from .library import JsonataLibrary, JsonataLibraryOptions, _ExportedJsonataFunction
from .loader.loader import CompiledModule, ExpressionLoader
from .optimizer.optimizer import optimize
from .parser.parser import Parser

# $eval compiles at evaluation time; compilation costs low milliseconds even
# in Python, so a bounded LRU cache still pays for itself for tight loops
# calling $eval on the same expression text repeatedly.
#
# Bounded by BOTH an entry count and a total key-text size. $eval's
# argument is an ordinary JSONata value, so a document can supply the
# expression text and therefore control the key length: an entry count
# alone bounds how MANY expressions are retained, not how LARGE they are,
# which is the same trap tier 2 below already guards against.
_EVAL_CACHE_LIMIT = 256
_EVAL_CACHE_MAX_BYTES = 1 << 20

# Repeat compile() is served by two cache tiers, because the constraint
# they work under is not the same at both levels.
#
# Tier 1 (_compile_cache) holds a *weak* reference to the entry_point,
# which is a pure function with no runtime-mutated module state (the
# `#$pos` counter box is created fresh inside `_evaluate` on each call,
# and the hoisted `_keysN` constant lists are only ever read), so the same
# entry_point can back many independent CompiledExpression wrappers. It
# must be weak: test_memory.py's collectibility contract requires that
# nothing in the factory pins a compiled expression's generated module
# alive once the caller drops its last reference. A hit here costs a dict
# lookup (~0.8us) -- but only while some earlier CompiledExpression for
# that text is still reachable.
#
# Tier 2 (_code_cache) is what catches the case tier 1 structurally
# cannot: a long-lived process that compiles an expression, uses it,
# drops it, and meets the same text again later. Measured, that fell all
# the way back to a full recompile (~10.5ms on the benchmark expression).
# A *code object* references neither a module namespace nor a
# CompiledExpression, so holding one strongly cannot keep a generated
# module alive and does not touch the contract above; re-executing it
# into a fresh namespace costs ~15us rather than ~10.5ms. Bounded separately and more tightly than tier 1,
# because a code object for a large expression retains ~40 KiB.
# Tier 1's values are weakrefs (cheap), but its KEYS are the expression
# text, so it is byte-bounded for the same reason.
_COMPILE_CACHE_LIMIT = 512
_COMPILE_CACHE_MAX_BYTES = 1 << 20
# Tier 2 is bounded by generated-source bytes, not by an entry count:
# generated modules differ in size by two orders of magnitude (a 190-line
# expression generates ~34 KB and retains ~50 KiB cached; `a.b.c`
# generates 193 bytes), so a bare entry count would be either useless for
# small expressions or a multi-megabyte surprise for large ones. The entry
# count is a secondary cap for the many-tiny-expressions case.
_CODE_CACHE_LIMIT = 128
_CODE_CACHE_MAX_BYTES = 1 << 20

# A deeply nested ("(((...)))") or very long ("1+1+...+1") expression can
# exhaust the interpreter stack: the parser, the optimizer and the translator
# all walk the tree recursively, and CPython's own compiler can overflow on the
# generated source too. compile() is routinely handed untrusted expression
# text, so surface that as the documented JsonataCompilationError instead of
# letting a raw RecursionError escape (evaluate() already maps it to U1001).
#
# This is deliberately a host-dependent limit -- it moves with
# sys.setrecursionlimit -- rather than a fixed depth cap in the parser. A fixed
# cap was considered and rejected: the parser can only see *syntactic* nesting,
# so it would not catch a flat "1+1+...+1" chain, whose 20000-deep left-nested
# AST parses fine and then overflows the optimizer. Any constant low enough to
# be deterministic on the default 1000-frame stack would also reject
# expressions that compile fine today.
_TOO_DEEP = "expression is too deeply nested to compile"


class _CacheEntry:
    __slots__ = ("entry_point_ref", "source_jsonata")

    def __init__(self, entry_point: Any, source_jsonata: str, expression: str) -> None:
        self.entry_point_ref = weakref.ref(entry_point)
        # _SOURCE is the expression text, which is already this entry's
        # cache key -- storing it again doubled what a full cache of large
        # expressions retained. Keep it only on the (currently
        # unreachable) path where it somehow differs.
        self.source_jsonata = "" if source_jsonata == expression else source_jsonata

    def source_for(self, expression: str) -> str:
        return self.source_jsonata or expression


_class_counter = itertools.count()


class JsonataExpressionFactory:
    def __init__(self) -> None:
        self._loader = ExpressionLoader()
        self._eval_cache: OrderedDict[str, CompiledExpression] = OrderedDict()
        self._eval_cache_bytes = 0
        self._compile_cache: OrderedDict[str, _CacheEntry] = OrderedDict()
        self._compile_cache_bytes = 0
        self._code_cache: OrderedDict[str, CompiledModule] = OrderedDict()
        self._code_cache_bytes = 0
        self._compile_cache_lock = threading.Lock()
        # Dedicated lock: the $eval cache is mutated from inside an
        # evaluation, so it must not contend with (or nest inside) the
        # compile-cache lock.
        self._eval_cache_lock = threading.Lock()

        def eval_delegate(expr: str, ctx: Any) -> Any:
            from .runtime.values import MISSING

            try:
                # The cache is shared mutable state on a factory documented
                # as thread-safe, so every read, insert, eviction and LRU
                # reorder has to be under the lock: an unlocked hit followed
                # by another thread's evicting insert would make
                # move_to_end() raise KeyError for a perfectly valid
                # expression. compile() runs *outside* the lock -- two
                # threads may then compile the same text concurrently, which
                # is merely wasteful (same trade-off as _instantiate).
                with self._eval_cache_lock:
                    compiled = self._eval_cache.get(expr)
                    if compiled is not None:
                        self._eval_cache.move_to_end(expr)
                if compiled is None:
                    compiled = self.compile(expr)
                    with self._eval_cache_lock:
                        # Only charge a key that was not already present:
                        # two threads can miss concurrently and both
                        # arrive here with the same text, and the second
                        # insert overwrites rather than adds.
                        if expr not in self._eval_cache:
                            self._eval_cache_bytes += len(expr)
                        self._eval_cache[expr] = compiled
                        self._eval_cache.move_to_end(expr)
                        while self._eval_cache and (
                            len(self._eval_cache) > _EVAL_CACHE_LIMIT
                            or self._eval_cache_bytes > _EVAL_CACHE_MAX_BYTES
                        ):
                            evicted_key, _ = self._eval_cache.popitem(last=False)
                            self._eval_cache_bytes -= len(evicted_key)
                return compiled.evaluate(None if ctx is MISSING else ctx)
            except JsonataCompilationError as e:
                # T1005 means the text parsed but named a non-function --
                # closer to "can't be evaluated" than "can't be parsed".
                if e.error_code == "T1005":
                    raise JsonataEvaluationError("D3121", "The expression cannot be evaluated", e) from e
                raise JsonataEvaluationError("D3120", "The expression cannot be parsed", e) from e
            except JsonataEvaluationError as e:
                raise JsonataEvaluationError("D3121", "The expression cannot be evaluated", e) from e

        self._eval_delegate = eval_delegate

    def _attach(self, expr: CompiledExpression) -> CompiledExpression:
        expr._set_eval_delegate(self._eval_delegate)
        return expr

    def translate(self, expression: str) -> str:
        """Parses, optimises, and translates expression, returning the
        generated Python source. Public and useful for debugging; the
        output format is unstable across versions.

        (This used to accept a module_name argument that was silently
        ignored: the generated source does not name its module. The module
        name is chosen by compile(), which passes it to the loader.)"""
        from .translator.translator import Translator

        try:
            ast = optimize(Parser.parse(expression))
        except ParseError as e:
            raise JsonataCompilationError(e.error_code, str(e.message), e) from e
        except RecursionError as e:
            raise JsonataCompilationError(None, _TOO_DEEP, e) from e
        try:
            return Translator.translate(ast, expression)
        except TranslatorError as e:
            raise JsonataCompilationError(e.error_code, str(e.message), e) from e
        except RecursionError as e:
            raise JsonataCompilationError(None, _TOO_DEEP, e) from e

    def compile(self, expression: str) -> CompiledExpression:
        """Compiles expression to a reusable CompiledExpression.

        Repeat calls with the same expression text skip work: they reuse
        the cached entry_point while one is still alive, and otherwise
        re-execute a cached code object rather than running the pipeline
        again (see _COMPILE_CACHE_LIMIT / _CODE_CACHE_LIMIT). Each call
        still gets its own CompiledExpression wrapper, so per-instance
        state (assign/register_function/timeout) is never shared between
        callers.

        Raises JsonataCompilationError if the expression is syntactically
        invalid or the generated Python code cannot be compiled.
        """
        entry_point = None
        source_jsonata = ""
        with self._compile_cache_lock:
            cached = self._compile_cache.get(expression)
            if cached is not None:
                entry_point = cached.entry_point_ref()
                if entry_point is None:
                    del self._compile_cache[expression]
                    self._compile_cache_bytes -= len(expression)
                else:
                    source_jsonata = cached.source_for(expression)
                    self._compile_cache.move_to_end(expression)
        if entry_point is None:
            entry_point, source_jsonata = self._instantiate(expression)
            with self._compile_cache_lock:
                # See the eval cache: charge the key only when it is new,
                # since concurrent misses can both reach this insert.
                if expression not in self._compile_cache:
                    self._compile_cache_bytes += len(expression)
                self._compile_cache[expression] = _CacheEntry(entry_point, source_jsonata, expression)
                self._compile_cache.move_to_end(expression)
                while self._compile_cache and (
                    len(self._compile_cache) > _COMPILE_CACHE_LIMIT
                    or self._compile_cache_bytes > _COMPILE_CACHE_MAX_BYTES
                ):
                    evicted_key, _ = self._compile_cache.popitem(last=False)
                    self._compile_cache_bytes -= len(evicted_key)
        return self._attach(CompiledExpression(entry_point, source_jsonata or expression))

    def _instantiate(self, expression: str) -> tuple[Any, str]:
        """Produces a fresh (entry_point, source_jsonata) for expression,
        running the pipeline only if no code object is cached for it."""
        with self._compile_cache_lock:
            compiled = self._code_cache.get(expression)
            if compiled is not None:
                self._code_cache.move_to_end(expression)
        if compiled is None:
            module_name = f"expr{next(_class_counter)}"
            src = self.translate(expression)
            try:
                compiled = self._loader.compile_source(src, module_name)
            except LoadError as e:
                raise JsonataCompilationError(
                    None, f"Failed to load generated module for expression: {e.message}", e
                ) from e
            except RecursionError as e:
                raise JsonataCompilationError(None, _TOO_DEEP, e) from e
            with self._compile_cache_lock:
                # Two threads can miss concurrently and both arrive here;
                # discount whatever the loser's entry was accounted at.
                superseded = self._code_cache.get(expression)
                if superseded is not None:
                    self._code_cache_bytes -= superseded.source_size
                self._code_cache[expression] = compiled
                self._code_cache.move_to_end(expression)
                self._code_cache_bytes += compiled.source_size
                while self._code_cache and (
                    len(self._code_cache) > _CODE_CACHE_LIMIT
                    or self._code_cache_bytes > _CODE_CACHE_MAX_BYTES
                ):
                    _, evicted = self._code_cache.popitem(last=False)
                    self._code_cache_bytes -= evicted.source_size
        try:
            return self._loader.instantiate(compiled)
        except LoadError as e:
            raise JsonataCompilationError(
                None, f"Failed to load generated module for expression: {e.message}", e
            ) from e
        except RecursionError as e:
            raise JsonataCompilationError(None, _TOO_DEEP, e) from e

    def compile_all(self, expressions: Sequence[str]) -> list[CompiledExpression]:
        """As compile(), for a batch. Not the 10x win it is on the JVM (see
        loader.py) -- kept only so calling code can be written once against
        both this and the Java library."""
        results = []
        for i, expr in enumerate(expressions):
            try:
                results.append(self.compile(expr))
            except JsonataCompilationError as e:
                raise JsonataCompilationError(
                    e.error_code, f"Failed to compile expression [{i}] ({expr}): {e.message}", e
                ) from e
        return results

    def compile_library(self, definition: str, options: JsonataLibraryOptions | None = None) -> JsonataLibrary:
        """Compiles a JSONata *library*: a definition expression, and
        everything it exports.

        A definition expression is ordinary JSONata. It binds names --
        functions, values, or both -- and returns the names to export as
        an array of strings. See library.py for the full contract.

        The definition is compiled and evaluated once, here. Each
        exported name goes to JsonataLibrary.functions or .constants
        according to what it evaluated to.

        Raises JsonataCompilationError if the definition cannot be
        compiled, does not return a usable array of names, or names
        something it does not bind at its top level.
        """
        opts = options if options is not None else JsonataLibraryOptions()

        try:
            ast = Parser.parse(definition)
        except ParseError as e:
            raise JsonataCompilationError(e.error_code, f"Invalid JSONata expression: {e.message}", e) from e

        top_level = _export_rewriter.top_level_bindings(ast)
        _export_rewriter.require_self_contained(ast, _provided_names(opts))

        try:
            rewritten = _export_rewriter.rewrite(ast, list(top_level.keys()))
            optimized = optimize(rewritten)
            from .translator.translator import Translator

            source = Translator.translate(optimized, definition)
        except TranslatorError as e:
            raise JsonataCompilationError(e.error_code, f"Failed to translate definition expression: {e.message}", e) from e

        try:
            entry_point, source_jsonata = self._loader.load(source)
        except LoadError as e:
            raise JsonataCompilationError(
                None, f"Failed to load generated module for definition expression: {e.message}", e
            ) from e
        definition_expr = self._attach(CompiledExpression(entry_point, source_jsonata or definition))

        library = JsonataLibrary(definition, definition_expr, opts.bindings)

        try:
            # The definition runs exactly once. Its function values are
            # ordinary values, so the exported functions stay callable
            # for as long as the library is referenced.
            exported = definition_expr.evaluate(opts.input, opts.bindings)
            values = exported.get(_export_rewriter.FUNCTIONS_FIELD) if isinstance(exported, dict) else None
            names = exported.get(_export_rewriter.NAMES_FIELD) if isinstance(exported, dict) else None
            for name in _export_rewriter.exported_names(names):
                value = _exported_value(name, values, top_level)
                from .runtime.lambdas import is_lambda_token

                if is_lambda_token(value):
                    library._export_function(name, _wrap(name, value, top_level, opts, library))
                else:
                    library._export_constant(name, value)
        except JsonataEvaluationError as e:
            raise JsonataCompilationError(e.error_code, f"Definition expression failed to evaluate: {e.message}", e) from e
        return library


def _provided_names(options: JsonataLibraryOptions) -> set[str]:
    """The names the caller supplies at build time, which a definition
    may therefore rely on."""
    bindings = options.bindings
    if bindings is None:
        return set()
    return set(bindings.get_values()) | set(bindings.get_functions())


def _exported_value(name: str, values: Any, top_level_bindings: dict[str, Any]) -> Any:
    """Returns what name evaluated to, or explains precisely why it
    cannot be exported: the definition never bound it, or bound it to
    nothing.

    Checks key presence with `in`, not `.get(name) is not None` -- a
    binding legitimately exported as JSON null (D1: a real value, not
    absence) must round-trip as None, not be mistaken for "not exported"."""
    if isinstance(values, dict) and name in values:
        return values[name]
    if name in top_level_bindings:
        raise JsonataCompilationError(None, f"${name} is exported but evaluated to nothing in the definition expression")
    if top_level_bindings:
        bound = ", $".join(top_level_bindings.keys())
        raise JsonataCompilationError(
            None, f"${name} is exported but not defined at the top level of the definition expression (it binds: ${bound})"
        )
    raise JsonataCompilationError(
        None, f"${name} is exported but not defined at the top level of the definition expression (it binds no variables at all)"
    )


def _wrap(
    name: str, value: Any, top_level_bindings: dict[str, Any], opts: JsonataLibraryOptions, library: JsonataLibrary
) -> _ExportedJsonataFunction:
    """Wraps one exported function value, with the arity and signature
    recovered from the AST."""
    info = _export_rewriter.describe(top_level_bindings.get(name))
    override = opts.signature_override(name)
    return _ExportedJsonataFunction(name, value, override if override is not None else info.signature, info.arity, library)
