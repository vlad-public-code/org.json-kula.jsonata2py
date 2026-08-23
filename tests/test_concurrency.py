"""Concurrency guarantees (Phase 8 hardening gate, design spec section 9):

  * JsonataExpressionFactory and every CompiledExpression it produces are
    thread-safe; evaluate() is stateless.
  * Per-instance mutable state (assign/register_function) is guarded by a
    lock on writes; reads stay lock-free and see either the old or the
    new permanent-bindings snapshot, never a torn one.
  * Per-evaluation state lives in a contextvars.ContextVar (D5, not
    threading.local), so it is correct under asyncio as well as threads:
    a new thread starts with an empty context, and a nested evaluation
    ($eval, or a bound function that evaluates its own expression)
    suspends the outer one via a Frame chain instead of clobbering it.

No direct Java source file to port from (JsonataBindingsTest.java and
JsonataLibraryTest.java each embed a couple of concurrency tests inline,
already ported into test_bindings.py / test_library.py) -- this file is
the dedicated stress coverage the design spec calls out as its own gate.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading

import jsonata2py as jsonata
from jsonata2py.bindings import JsonataBindings, JsonataFunctionArguments

_FACTORY = jsonata.JsonataExpressionFactory()


# =============================================================================
# A single shared CompiledExpression under concurrent per-evaluation bindings
# =============================================================================


def test_shared_expression_many_threads_no_interference():
    expr = _FACTORY.compile("$rate * amount")
    threads = 64
    iterations = 50

    def worker(rate: float) -> int:
        errors = 0
        for _ in range(iterations):
            b = JsonataBindings().bind_value("rate", rate)
            result = expr.evaluate({"amount": 10}, b)
            if abs(result - rate * 10) > 1e-9:
                errors += 1
        return errors

    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as pool:
        futures = [pool.submit(worker, t + 1) for t in range(threads)]
        assert sum(f.result() for f in futures) == 0


def test_shared_expression_concurrent_evaluate_no_shared_state_input():
    # Same expression, same call, no bindings at all -- every thread must
    # see the same deterministic result; nothing in the generated module
    # is mutated by evaluate().
    expr = _FACTORY.compile("$string($sum([1..1000])) & '-' & $join(['a','b','c'], '-')")

    def worker(_: int) -> object:
        return expr.evaluate(None)

    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(worker, range(200)))
    assert all(r == "500500-a-b-c" for r in results)


# =============================================================================
# assign() / register_function() -- concurrent writes and reads
# =============================================================================


def test_assign_concurrent_reads_see_a_consistent_snapshot():
    expr = _FACTORY.compile("$multiplier * x")
    expr.assign("multiplier", 3)

    def worker(x: int) -> tuple[int, float]:
        return x, expr.evaluate({"x": x})

    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
        futures = [pool.submit(worker, x) for x in range(1, 33)]
        for f in futures:
            x, result = f.result()
            assert result == x * 3


def test_concurrent_assign_and_evaluate_never_crashes_or_tears():
    # Writers keep reassigning while readers keep evaluating; every read
    # must observe a whole permanent-bindings snapshot (some multiplier
    # that was actually assigned), never a half-written state, and no
    # exception should escape either side.
    expr = _FACTORY.compile("$multiplier * 10")
    expr.assign("multiplier", 1)
    stop = threading.Event()
    seen: set[int] = set()
    seen_lock = threading.Lock()
    errors: list[BaseException] = []

    def writer() -> None:
        m = 1
        while not stop.is_set():
            m += 1
            expr.assign("multiplier", m)

    def reader() -> None:
        try:
            for _ in range(2000):
                result = expr.evaluate(None)
                with seen_lock:
                    seen.add(result)
        except BaseException as e:
            errors.append(e)

    threads = [threading.Thread(target=writer) for _ in range(2)]
    threads += [threading.Thread(target=reader) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads[2:]:
        t.join()  # readers finish first
    stop.set()
    for t in threads[:2]:
        t.join()

    assert not errors
    # Every observed result must be a whole multiplier * 10 -- never a
    # value that could only arise from a torn read.
    assert all(v % 10 == 0 for v in seen)


# =============================================================================
# JsonataExpressionFactory -- concurrent compilation
# =============================================================================


def test_factory_compiles_many_distinct_expressions_concurrently():
    factory = jsonata.JsonataExpressionFactory()

    def worker(i: int) -> object:
        expr = factory.compile(f"$sum([1..{i}])")
        return expr.evaluate(None)

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(worker, range(1, 101)))
    for i, result in enumerate(results, start=1):
        assert result == i * (i + 1) // 2


def test_factory_compiles_the_same_expression_concurrently():
    factory = jsonata.JsonataExpressionFactory()

    def worker(_: int) -> object:
        return factory.compile("1 + 2 + 3").evaluate(None)

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(worker, range(100)))
    assert all(r == 6 for r in results)


# =============================================================================
# A new thread starts with an empty per-evaluation context, but permanent
# bindings live on the instance, not the context -- so they are still there.
# =============================================================================


def test_permanent_bindings_visible_from_a_fresh_thread():
    expr = _FACTORY.compile("$taxRate * amount")
    expr.assign("taxRate", 0.2)

    result_box: list[object] = []

    def worker() -> None:
        result_box.append(expr.evaluate({"amount": 100}))

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    assert result_box == [20.0]


# =============================================================================
# Nested evaluation ($eval) must not clobber the outer evaluation's frame
# =============================================================================


def test_nested_eval_does_not_clobber_the_outer_local_binding():
    expr = _FACTORY.compile("($x := 5; $ignored := $eval('1+1'); $x)")
    assert expr.evaluate(None) == 5


def test_nested_eval_sees_its_own_context_not_the_outers_bindings():
    # $eval's second argument is the context for the sub-expression; a
    # bare $x inside it must NOT resolve to the outer block's $x -- $eval
    # opens a genuinely separate frame.
    expr = _FACTORY.compile('($x := 5; $eval("$exists($x)"))')
    assert expr.evaluate(None) is False


# =============================================================================
# asyncio -- ContextVar isolation across concurrently scheduled tasks
# =============================================================================


def test_asyncio_tasks_do_not_leak_bindings_across_each_other():
    expr = _FACTORY.compile("$rate * amount")

    async def one_task(rate: float) -> float:
        loop = asyncio.get_event_loop()
        b = JsonataBindings().bind_value("rate", rate)
        # run_in_executor hands this off to a real thread, exercising the
        # same ContextVar-per-thread isolation as the pure-threading tests
        # above, but reached through asyncio's scheduling.
        return await loop.run_in_executor(None, expr.evaluate, {"amount": 10}, b)

    async def main() -> list[float]:
        return await asyncio.gather(*(one_task(rate) for rate in range(1, 33)))

    results = asyncio.run(main())
    assert results == [rate * 10 for rate in range(1, 33)]


# =============================================================================
# The $eval expression cache under concurrency
# =============================================================================
#
# eval_delegate keeps a bounded LRU of expressions compiled by $eval. It used
# to read, insert, evict and reorder that OrderedDict with no lock at all,
# while the compile/code caches beside it were locked. The interleaving that
# bites: thread A gets a hit for `expr`, thread B's insert evicts `expr`, and
# A's move_to_end(expr) then raises KeyError -- surfacing as a spurious
# JsonataEvaluationError for a valid expression. The window is one bytecode
# boundary wide, so this test is a smoke gate, not a reliable reproducer: it
# rotates far more expression texts than the cache holds, so eviction runs
# constantly for the whole run.


def test_concurrent_eval_with_constant_cache_eviction():
    from jsonata2py.factory import _EVAL_CACHE_LIMIT

    factory = jsonata.JsonataExpressionFactory()
    # More distinct texts than the cache can hold => every miss evicts.
    texts = [f"{i} + 1" for i in range(_EVAL_CACHE_LIMIT * 2)]
    expr = factory.compile("$eval($text)")
    threads = 16
    per_thread = 300

    def worker(offset: int) -> list[float]:
        out = []
        for i in range(per_thread):
            text = texts[(offset + i) % len(texts)]
            b = JsonataBindings().bind_value("text", text)
            out.append(expr.evaluate(None, b))
        return out

    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as pool:
        futures = [pool.submit(worker, t * 37) for t in range(threads)]
        results = [f.result() for f in futures]

    for t, out in enumerate(results):
        expected = [int(texts[(t * 37 + i) % len(texts)].split(" + ")[0]) + 1 for i in range(per_thread)]
        assert out == expected


def test_concurrent_eval_of_the_same_text_agrees():
    """One cache entry, hit from every thread at once -- the pure-hit path,
    where the LRU reorder used to run unlocked. ($eval evaluates against the
    context, not the enclosing bindings -- see the $exists test above -- so
    the varying input arrives as data.)"""
    factory = jsonata.JsonataExpressionFactory()
    expr = factory.compile('$eval("x * 2")')

    def worker(x: int) -> float:
        return expr.evaluate({"x": x})

    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
        results = list(pool.map(worker, range(200)))

    assert results == [x * 2 for x in range(200)]


# =============================================================================
# compile_library() under concurrency
# =============================================================================
#
# compile_library passes no module name, so the loader mints one itself. That
# counter used to be a plain class attribute incremented with +=, which is a
# non-atomic load/add/store: two threads could mint the same module name for
# *different* sources. _register_linecache's refcounted eviction assumes one
# <jsonata:exprN> filename maps to exactly one source, so the collision would
# make tracebacks and inspect.getsource() show the wrong generated code.


def test_concurrent_compile_library_mints_distinct_modules():
    factory = jsonata.JsonataExpressionFactory()
    count = 200

    def worker(n: int):
        definition = f"($add{n} := function($x){{ $x + {n} }}; [\"add{n}\"])"
        lib = factory.compile_library(definition)
        fn = lib.functions[f"add{n}"]
        return n, fn.apply(JsonataFunctionArguments([1]))

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(worker, range(count)))

    assert results == [(n, n + 1) for n in range(count)]


def test_loader_module_names_are_unique_under_threads():
    from jsonata2py.loader.loader import ExpressionLoader

    loader = ExpressionLoader()
    source = "def _evaluate(_root, _ctx=None):\n    return 1\n"
    names: list[str] = []
    lock = threading.Lock()

    def worker() -> None:
        minted = [loader.compile_source(source).module_name for _ in range(200)]
        with lock:
            names.extend(minted)

    workers = [threading.Thread(target=worker) for _ in range(8)]
    for w in workers:
        w.start()
    for w in workers:
        w.join()

    assert len(names) == 1600
    assert len(set(names)) == len(names), "two threads minted the same module name"
