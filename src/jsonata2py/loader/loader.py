"""Compiles Python source strings into CompiledExpression instances.

Ported from org.json_kula.jsonata_jvm.loader.JsonataExpressionLoader (D2).

Java's in-memory javac + classloader machinery (~150-800ms per invocation,
which is why compileAll/loadAll batch calls existed and were worth ~10x)
becomes `compile()` + `exec()`: microseconds to low milliseconds. The
entire reason compileAll existed on the JVM disappears -- compile_all is
kept here only for API parity (a loop), not performance.

Generated source is registered in linecache so a traceback out of a
generated expression shows the actual generated line, and
inspect.getsource works -- strictly better than the Java side, where a
stack trace from a generated class shows a line number against source
nobody kept.

Phase 8 hardening (test_memory.py): entries added to linecache.cache are a
genuine unbounded leak if never evicted. Eviction is precise, not
time-or-count-based: weakref.finalize on the module's `_evaluate` function
(the one thing a live CompiledExpression always holds a direct reference
to) pops the linecache entry once that function is garbage collected.
`_evaluate.__globals__ is ns` and `ns["_evaluate"] is entry_point` form a
reference cycle, so this normally isn't instant refcount-drop-to-zero
collection -- it resolves at the next cyclic gc pass, same as any other
self-referencing object graph. A bounded LRU cap on top is a pure
backstop for pathological cases (a caller holding entry_point directly
without a CompiledExpression wrapper, or before gc has run), not the
primary mechanism.
"""

from __future__ import annotations

import itertools
import linecache
import re
import threading
import weakref
from collections import OrderedDict
from typing import Any

from ..errors import LoadError

_CLASS_PATTERN = re.compile(r"(?m)^def\s+_evaluate\s*\(")

# The shape linecache.cache stores: (size, mtime, lines, fullname).
_LinecacheEntry = tuple[int, None, list[str], str]

# Generated modules open with a star import of the runtime (see
# translator/module_assembler.py) so that the source `translate()` hands
# back is standalone and runnable. Executing that import per module is not
# free: it binds ~175 names one at a time, measured at 21.1 us, which is
# 20% of the cost of compiling a small expression. Copying a namespace
# prepared once at import time is 2.7 us for the same result.
#
# The import line is blanked rather than removed so that every subsequent
# line keeps its number and the linecache entry registered below still
# lines up with what a traceback reports.
_RUNTIME_IMPORT = "from jsonata2py.runtime import *"


# Built on first use, not at import time: the loader is imported from
# factory.py, and binding the runtime here eagerly would add an
# import-order constraint for no benefit.
_RUNTIME_NS: dict[str, Any] | None = None


def _runtime_namespace() -> dict[str, Any]:
    global _RUNTIME_NS
    ns = _RUNTIME_NS
    if ns is None:
        ns = {}
        exec(f"{_RUNTIME_IMPORT}\n", ns)
        _RUNTIME_NS = ns
    return ns


# Backstop bound on registered linecache entries (see module docstring --
# weakref.finalize does the real eviction; this just caps worst-case growth).
_LINECACHE_MAX_ENTRIES = 2000
_linecache_keys: OrderedDict[str, None] = OrderedDict()
# One code object can be instantiated into many independent modules (the
# factory caches code objects), and they all share a filename. Evicting on
# the first entry_point to be collected would pull the source out from
# under the ones still alive, so eviction is refcounted: the entry goes
# when the last instantiation does.
_linecache_refs: dict[str, int] = {}
# Must be REENTRANT. A garbage collection can start at any allocation point,
# including inside the locked section below, and weakref.finalize callbacks
# run on whichever thread triggered the collection. That callback is
# _evict_linecache, which takes this same lock -- with a plain Lock the thread
# deadlocks against itself, permanently wedging every compile that follows.
# Reproduced deterministically on CPython 3.12 (heavy compile churn wedges at
# ~850 expressions); latent on every other version, where it depends purely on
# when the collector happens to run.
_linecache_lock = threading.RLock()


def _linecache_entry(filename: str, source: str) -> _LinecacheEntry:
    """Builds the linecache tuple once, so that re-instantiating a cached
    code object shares one split-lines list instead of rebuilding a
    ~31 KiB one per instantiation."""
    return (len(source), None, source.splitlines(True), filename)


def _register_linecache(filename: str, entry: _LinecacheEntry, entry_point: Any) -> None:
    with _linecache_lock:
        linecache.cache[filename] = entry
        _linecache_keys[filename] = None
        _linecache_keys.move_to_end(filename)
        _linecache_refs[filename] = _linecache_refs.get(filename, 0) + 1
        while len(_linecache_keys) > _LINECACHE_MAX_ENTRIES:
            oldest, _ = _linecache_keys.popitem(last=False)
            linecache.cache.pop(oldest, None)
            _linecache_refs.pop(oldest, None)

    weakref.finalize(entry_point, _evict_linecache, filename)


def _evict_linecache(filename: str) -> None:
    with _linecache_lock:
        remaining = _linecache_refs.get(filename, 0) - 1
        if remaining > 0:
            _linecache_refs[filename] = remaining
            return
        _linecache_refs.pop(filename, None)
        linecache.cache.pop(filename, None)
        _linecache_keys.pop(filename, None)


class CompiledModule:
    """A compiled-but-not-yet-executed generated module.

    Splitting compile() from exec() is what lets the factory cache the
    expensive half. A code object references neither a module namespace
    nor a CompiledExpression, so caching one cannot keep a generated
    module alive -- which is exactly the constraint test_memory.py
    enforces, and the reason the factory's other cache holds only a
    weak reference.
    """

    linecache_entry: _LinecacheEntry

    __slots__ = ("code", "filename", "linecache_entry", "module_name", "source_size")

    def __init__(self, code: Any, source: str, filename: str, module_name: str) -> None:
        self.code = code
        self.filename = filename
        self.module_name = module_name
        # Prebuilt so every instantiation shares one lines list.
        self.linecache_entry = _linecache_entry(filename, source)
        # What a cache holding this retains, near enough: callers bound
        # their caches on it rather than on a bare entry count, because
        # generated sources differ in size by two orders of magnitude.
        self.source_size = len(source)


class ExpressionLoader:
    """Compiles generated Python source into callables.

    Not thread-unsafe in itself -- compile()/exec() are stateless -- but
    each instantiate() call creates a fresh, independent module namespace.
    """

    # An itertools.count, not an int: next() on one is a single C-level step,
    # whereas the load/add/store of `_counter += 1` is not atomic. Two threads
    # racing that could mint the same module name for *different* sources, and
    # _register_linecache's refcounted eviction assumes one
    # <jsonata:exprN> filename maps to exactly one source -- so tracebacks and
    # inspect.getsource() would show the wrong generated code. Reachable via
    # concurrent compile_library(), which passes no module name.
    _counter = itertools.count(1)

    def compile_source(self, source: str, module_name: str | None = None) -> CompiledModule:
        """Compiles source to a reusable CompiledModule without executing
        it.

        Raises LoadError if the source cannot be compiled or does not
        declare a top-level `_evaluate` function.
        """
        if module_name is None:
            module_name = f"expr{next(ExpressionLoader._counter)}"

        if not _CLASS_PATTERN.search(source):
            raise LoadError("Cannot find a `def _evaluate(...)` declaration in the provided source.")

        filename = f"<jsonata:{module_name}>"
        # Blank the star import and pre-populate the namespace instead --
        # same names, same line numbers, ~18us cheaper. Sources that do
        # not carry the import are unaffected: they simply find the
        # runtime names already bound.
        executable = source.replace(_RUNTIME_IMPORT, "", 1)
        try:
            code = compile(executable, filename, "exec")
        except SyntaxError as e:
            raise LoadError(f"Failed to compile generated source for '{module_name}': {e}", e) from e
        return CompiledModule(code, source, filename, module_name)

    def instantiate(self, compiled: CompiledModule) -> tuple[Any, str]:
        """Executes a CompiledModule into a fresh namespace and returns
        (entry_point, source_jsonata).

        Safe to call many times on the same CompiledModule: each call
        produces an independent module namespace and an independent
        `_evaluate`, so no state is shared between the results.
        """
        ns: dict[str, Any] = _runtime_namespace().copy()
        try:
            exec(compiled.code, ns)
        except Exception as e:
            raise LoadError(f"Failed to execute generated module '{compiled.module_name}': {e}", e) from e

        entry_point = ns.get("_evaluate")
        if entry_point is None:
            raise LoadError(f"Generated module '{compiled.module_name}' does not define `_evaluate`.")
        _register_linecache(compiled.filename, compiled.linecache_entry, entry_point)

        return entry_point, ns.get("_SOURCE", "")

    def load(self, source: str, module_name: str | None = None) -> tuple[Any, str]:
        """Compiles source and returns (entry_point, source_jsonata) where
        entry_point is the module's `_evaluate` callable.

        Raises LoadError if the source cannot be compiled or does not
        declare a top-level `_evaluate` function.
        """
        return self.instantiate(self.compile_source(source, module_name))

    def load_all(self, sources: list[str], module_names: list[str] | None = None) -> list[tuple[Any, str]]:
        """As load(), for a batch. Implemented as a loop -- see module
        docstring for why this is not the 10x win it is on the JVM."""
        if module_names is not None and len(module_names) != len(sources):
            raise LoadError(f"Got {len(module_names)} module names for {len(sources)} sources.")
        result = []
        for i, source in enumerate(sources):
            name = module_names[i] if module_names is not None else None
            result.append(self.load(source, name))
        return result
