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
# Two DIFFERENT compiled expressions, evaluated in parallel
# =============================================================================


def test_two_expressions_four_threads_in_parallel():
    """Four threads, two distinct pre-compiled expressions, interleaved.

    The tests above share one expression between threads, or compile many
    expressions concurrently and evaluate each once. Neither covers the shape
    a real service actually runs: a handful of long-lived compiled
    expressions, called concurrently and *alternately* on the same threads.

    That interleaving is what makes this more than a repeat of the first
    test. Per-evaluation state (the bindings overlay, the deadline, the call
    depth) lives in one `contextvars.ContextVar` shared by every expression,
    so a frame left behind by expression A would be seen by expression B on
    the same thread, not by another thread. Alternating A/B/A/B within a
    worker is the only arrangement that exercises that, and the two
    expressions are given *different binding names* so a leaked frame shows
    up as a wrong answer rather than a coincidentally-equal one.

    Verified to fail (not merely to pass): replacing the ContextVar with a
    process-global slot makes every thread read every other thread's
    `$minQty` and `$sep`, and this assertion reports it immediately.
    """
    totals = _FACTORY.compile("$sum(items[qty >= $minQty].(price * qty))")
    labels = _FACTORY.compile("$join(items[qty >= $minQty].name, $sep)")

    items = [{"name": f"i{i}", "price": i * 2, "qty": i % 6} for i in range(1, 25)]
    data = {"items": items}

    def expected_total(min_qty: int) -> float:
        return sum(i["price"] * i["qty"] for i in items if i["qty"] >= min_qty)

    def expected_label(min_qty: int, sep: str) -> object:
        names = [i["name"] for i in items if i["qty"] >= min_qty]
        return sep.join(names)

    threads = 4
    iterations = 250
    start = threading.Barrier(threads)
    failures: list[str] = []
    lock = threading.Lock()

    def worker(tid: int) -> None:
        min_qty = tid + 1  # a different filter per thread
        sep = f"<{tid}>"
        local: list[str] = []
        start.wait()  # all four genuinely in flight together
        for _ in range(iterations):
            got_total = totals.evaluate(data, JsonataBindings().bind_value("minQty", min_qty))
            if got_total != expected_total(min_qty):
                local.append(f"t{tid}: total {got_total!r} != {expected_total(min_qty)!r}")

            binds = JsonataBindings().bind_value("minQty", min_qty).bind_value("sep", sep)
            got_label = labels.evaluate(data, binds)
            if got_label != expected_label(min_qty, sep):
                local.append(f"t{tid}: label {got_label!r} != {expected_label(min_qty, sep)!r}")
        if local:
            with lock:
                failures.extend(local)

    workers = [threading.Thread(target=worker, args=(t,)) for t in range(threads)]
    for t in workers:
        t.start()
    for t in workers:
        t.join()

    assert not failures, failures[:10]


def test_two_expressions_four_threads_do_not_see_each_others_bindings():
    """`$sep` is bound only for the second expression, so its *absence* in
    the first is the assertion -- an unbound variable in JSONata is simply
    undefined.

    This catches a strictly different fault from the test above, which is
    why both are here. That one binds the same names in both expressions, so
    a lookup that resolved from the wrong frame would still find the right
    value; only a test where one expression binds a name the other does not
    can see it. Verified by injection: leaving the per-evaluation frame
    unpopped *and* letting lookup fall through to the suspended outer frame
    makes `$sep` visible here, while the test above stays green.
    """
    reads_sep = _FACTORY.compile("$exists($sep)")
    binds_sep = _FACTORY.compile("$sep & '!'")

    threads = 4
    iterations = 200
    start = threading.Barrier(threads)
    leaked: list[object] = []
    lock = threading.Lock()

    def worker(tid: int) -> None:
        start.wait()
        for _ in range(iterations):
            if binds_sep.evaluate(None, JsonataBindings().bind_value("sep", f"s{tid}")) != f"s{tid}!":
                with lock:
                    leaked.append(("wrong value", tid))
            saw = reads_sep.evaluate(None)  # no bindings at all
            if saw is not False:
                with lock:
                    leaked.append(("leaked $sep", tid, saw))

    workers = [threading.Thread(target=worker, args=(t,)) for t in range(threads)]
    for t in workers:
        t.start()
    for t in workers:
        t.join()

    assert not leaked, leaked[:10]


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
