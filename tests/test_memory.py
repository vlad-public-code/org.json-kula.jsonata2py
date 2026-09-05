"""Memory-hygiene tests (Phase 8 hardening gate).

Covers the two risks the design spec calls out by name (section 13):

  * The `linecache` leak -- entries registered so tracebacks/inspect.
    getsource work on generated code must not grow without bound in a
    long-lived process that compiles many expressions. loader.py resolves
    this with weakref.finalize on the module's `_evaluate` function plus a
    bounded-LRU backstop; this file verifies both actually fire.
  * A generated CompiledExpression must be fully collectible -- nothing in
    the loader or factory should pin it (or its generated module/closure
    graph) alive forever once the caller drops its last reference.

Also includes a scaled-down port of MemoryLeakStressTest.java (Java: 10
threads x 10,000 unique expressions x 100 evals each, `@Disabled` because
it takes minutes and is meant to be watched with a profiler, not asserted
on). The Python version keeps the same shape -- many threads, each
compiling many *distinct* expressions (a fresh regex + two lambdas each,
so nothing is cache-shared) and evaluating each many times against random
input -- at a size that finishes in CI, asserting correctness throughout
(a leak in this library would surface as growing latency or a crash under
this pattern, not a wrong answer, but the correctness check is what a
regression in the underlying logic would break first).
"""

from __future__ import annotations

import concurrent.futures
import gc
import linecache
import random
import threading
import weakref

import jsonata2py as jsonata
from jsonata2py.loader.loader import _LINECACHE_MAX_ENTRIES, _linecache_keys, _linecache_lock

_FACTORY = jsonata.JsonataExpressionFactory()


# =============================================================================
# linecache eviction
# =============================================================================


def test_linecache_entry_is_registered_on_compile():
    expr = _FACTORY.compile("1 + 1")
    filenames = [f for f in linecache.cache if "expr" in f]
    assert filenames, "expected at least one <jsonata:...> entry in linecache"
    del expr


def test_linecache_entry_is_evicted_once_the_expression_is_collected():
    before = set(linecache.cache)
    expr = _FACTORY.compile("2 + 2")
    during = set(linecache.cache) - before
    assert during, "compiling should have added a new linecache entry"

    del expr
    gc.collect()  # entry_point.__globals__ back-references entry_point -- needs a cyclic GC pass

    after = set(linecache.cache)
    assert not (during & after), "linecache entry should be evicted once nothing references the compiled expression"


def test_linecache_stays_bounded_under_heavy_compile_churn():
    # Compile far more expressions than the backstop cap, without ever
    # holding on to more than one at a time, and confirm the cache doesn't
    # grow past its bound even before a gc pass -- the LRU backstop, not
    # just precise eviction, must hold on its own.
    for i in range(_LINECACHE_MAX_ENTRIES + 500):
        _FACTORY.compile(f"{i} + 1")
    assert len(linecache.cache) <= _LINECACHE_MAX_ENTRIES + 50  # small slack for entries from other tests
    assert len(_linecache_keys) <= _LINECACHE_MAX_ENTRIES



def test_linecache_lock_is_reentrant():
    """The linecache lock MUST be reentrant, and this is not cosmetic.

    A garbage collection can begin at any allocation point, including inside
    the locked section of _register_linecache, and weakref.finalize callbacks
    run on whichever thread triggered that collection. The callback here is
    _evict_linecache, which takes this same lock. With a plain threading.Lock
    the thread deadlocks against itself and every subsequent compile() wedges
    forever.

    This reproduced deterministically on CPython 3.12 (heavy compile churn
    wedged at ~850 expressions) and was latent everywhere else, where it
    depended entirely on when the collector happened to run -- which is
    exactly why it needs a test that does not depend on GC timing.
    """
    assert _linecache_lock.acquire(blocking=False)
    try:
        # The re-entrant acquire is the whole point: a plain Lock returns
        # False here (and blocks forever in the real callback path).
        assert _linecache_lock.acquire(blocking=False), "linecache lock is not reentrant"
        _linecache_lock.release()
    finally:
        _linecache_lock.release()

# =============================================================================
# CompiledExpression / generated module collectibility
# =============================================================================


def test_compiled_expression_is_garbage_collected_when_dropped():
    factory = jsonata.JsonataExpressionFactory()
    expr = factory.compile("1 + 1")
    ref = weakref.ref(expr)

    del expr
    gc.collect()

    assert ref() is None, "CompiledExpression should be collectible once the caller drops its last reference"


def test_entry_point_function_is_garbage_collected_when_dropped():
    expr = _FACTORY.compile("1 + 1")
    entry_point = expr._entry_point
    ref = weakref.ref(entry_point)

    del expr
    del entry_point
    gc.collect()

    assert ref() is None, "the generated module's _evaluate function should be collectible"


def test_factory_itself_does_not_pin_compiled_expressions_alive():
    # The factory's $eval delegate closes over `self`, not over any
    # particular compiled expression -- a factory that outlives a compiled
    # expression must not keep it reachable.
    factory = jsonata.JsonataExpressionFactory()
    expr = factory.compile("1 + 1")
    ref = weakref.ref(expr)

    del expr
    gc.collect()

    assert ref() is None
    # The factory itself is still perfectly usable afterwards.
    assert factory.compile("3 + 4").evaluate(None) == 7


# =============================================================================
# Leak shape 1: ONE expression, evaluated a great many times
# =============================================================================


def _live_objects() -> int:
    """Tracked objects after a full collection.

    Two passes: the first can resurrect nothing but does free cycles whose
    finalizers create more garbage, and the generated modules here are
    exactly that shape (a function whose __globals__ references it back).
    """
    gc.collect()
    gc.collect()
    return len(gc.get_objects())


def test_single_expression_repeated_evaluation_does_not_accumulate():
    """The hot-path shape: compile once at startup, evaluate forever.

    Anything retained per evaluation -- a binding frame not popped, a
    sequence cached on the module, a deadline never cleared -- grows without
    bound here and nowhere else, because every other test drops the
    expression long before the retention could show.

    The assertion is on *growth between two phases of different size*, not on
    a single absolute number. A leak is proportional to the iteration count,
    so 8 000 evaluations must not cost four times what 2 000 did; a fixed
    warm-up cost is invisible to that comparison, which is what makes it
    robust enough to assert in CI.
    """
    expr = _FACTORY.compile("$sum(items[price > 10].(price * qty)) & '/' & $count(items[qty = 0])")
    data = {"items": [{"price": i, "qty": i % 5} for i in range(50)]}

    for _ in range(200):  # let one-time caches fill
        expr.evaluate(data)

    baseline = _live_objects()
    for _ in range(2_000):
        expr.evaluate(data)
    after_small = _live_objects()
    for _ in range(8_000):
        expr.evaluate(data)
    after_large = _live_objects()

    small_growth = after_small - baseline
    large_growth = after_large - after_small

    # Measured behaviour is exactly zero growth in both phases; the slack is
    # for objects other tests in the same process happen to leave behind.
    assert small_growth < 200, f"2 000 evaluations retained {small_growth} objects"
    assert large_growth < 200, f"8 000 further evaluations retained {large_growth} objects"
    # And the shape that matters: 4x the work must not cost ~4x the objects.
    assert large_growth <= max(small_growth, 20) * 2, (
        f"retention scales with iteration count: {small_growth} objects for 2 000 evaluations, "
        f"{large_growth} for 8 000 -- that is a leak, not a warm-up cost"
    )

    # The expression is still correct, and still collectible afterwards.
    assert expr.evaluate(data) is not None
    ref = weakref.ref(expr)
    del expr
    gc.collect()
    assert ref() is None


def test_single_expression_repeated_evaluation_under_threads_does_not_accumulate():
    """Same shape, but the per-evaluation state lives in a ContextVar, and a
    thread that finishes must not leave its frame stack behind."""
    expr = _FACTORY.compile("$sum(items.price) * $factor")
    data = {"items": [{"price": i} for i in range(40)]}
    expected = sum(range(40))

    def worker(factor: int) -> int:
        bindings = jsonata.JsonataBindings().bind_value("factor", factor)
        bad = 0
        for _ in range(500):
            if expr.evaluate(data, bindings) != expected * factor:
                bad += 1
        return bad

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(worker, range(1, 5)))  # warm

    baseline = _live_objects()
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(worker, range(1, 5))) == 0
    growth = _live_objects() - baseline

    assert growth < 400, f"4 threads x 500 evaluations retained {growth} objects"


# =============================================================================
# Leak shape 2: MANY distinct expressions, each evaluated once
# =============================================================================


def test_many_distinct_expressions_each_used_once_are_all_collectible():
    """The other end of the same axis: a process that compiles constantly.

    Every compiled expression carries a generated module, a code object, an
    entry-point function whose `__globals__` points back at it, and a
    linecache registration. If any of those pins the expression, this is
    where it shows -- so the primary assertion is that *not one* of several
    hundred survives a collection once the caller has dropped it.

    Growth is then checked the same way as the single-expression test: by
    comparing two run sizes rather than a single number. The factory keeps a
    bounded code-object cache, so some growth is correct and expected; what
    would be wrong is growth proportional to the number of expressions.
    """
    n = 300

    def run(count: int, seed: str) -> tuple[int, int]:
        """Compiles `count` distinct expressions, evaluates each once, drops
        it, and returns (survivors, object growth).

        The weakrefs are counted and then dropped *before* the second object
        measurement: a `weakref.ref` is itself a tracked object, so holding
        `count` of them shows up as exactly one object per expression --
        growth that looks precisely like a per-expression leak and is in fact
        this test's own bookkeeping.
        """
        refs: list[weakref.ref] = []
        before = _live_objects()
        for i in range(count):
            expr = _FACTORY.compile(f"$sum([1..{i + 1}]) + {i} & '{seed}'")
            refs.append(weakref.ref(expr))
            expr.evaluate(None)
            del expr
        gc.collect()
        survivors = sum(1 for r in refs if r() is not None)
        refs.clear()
        return survivors, _live_objects() - before

    # Warm until growth settles, then measure. The factory's code-object cache
    # is bounded by generated-source *bytes*, so it fills over the first couple
    # of runs -- ~1 800 objects, then ~400, then zero from there on. That is a
    # one-time cost, not a leak, but it is also why a fixed warm-up count would
    # be a guess: this loop saturates the cache however large it is, and makes
    # the test give the same answer run alone as it does mid-suite.
    for attempt in range(6):
        if run(n * 3, f"warm{attempt}")[1] == 0:
            break

    survivors_1x, growth_1x = run(n, "a")
    survivors_3x, growth_3x = run(n * 3, "b")

    assert survivors_1x == 0, f"{survivors_1x} of {n} compiled expressions survived being dropped"
    assert survivors_3x == 0, f"{survivors_3x} of {n * 3} compiled expressions survived being dropped"

    # Measured growth is 0 for both run sizes once warm. The absolute bound is
    # slack for objects other tests in the same process leave behind; the
    # scaling bound is the one that would catch a real per-expression leak.
    assert growth_1x < 200, f"{n} compile-and-discard cycles retained {growth_1x} objects"
    scaled = f"{growth_1x} objects for {n} expressions, {growth_3x} for {n * 3}"
    assert growth_3x <= max(growth_1x, 200) * 2, f"object growth scales with expression count: {scaled}"


def test_many_distinct_expressions_each_used_once_keep_linecache_bounded():
    """The registration made for tracebacks is per generated module, so this
    is the path that grows it. `_linecache_keys` is the library's own LRU
    backstop; `linecache.cache` is the global it must not be allowed to
    inflate."""
    for i in range(_LINECACHE_MAX_ENTRIES + 300):
        expr = _FACTORY.compile(f"'e{i}' & $string({i} * 2)")
        assert expr.evaluate(None) == f"e{i}{i * 2}"
        del expr

    assert len(_linecache_keys) <= _LINECACHE_MAX_ENTRIES
    assert len(linecache.cache) <= _LINECACHE_MAX_ENTRIES + 50


def test_many_distinct_expressions_each_used_once_across_threads():
    """Compile-and-discard from four threads at once: the loader mints module
    names and registers linecache entries under a lock, and a race there
    would either cross-wire two expressions (caught by the value check) or
    strand an entry (caught by the survivor check)."""
    n_per_thread = 120
    lock = threading.Lock()
    refs: list[weakref.ref] = []

    def worker(tid: int) -> int:
        bad = 0
        local: list[weakref.ref] = []
        for i in range(n_per_thread):
            expr = _FACTORY.compile(f"{tid} * 1000 + {i}")
            local.append(weakref.ref(expr))
            if expr.evaluate(None) != tid * 1000 + i:
                bad += 1
            del expr
        with lock:
            refs.extend(local)
        return bad

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        assert sum(pool.map(worker, range(4))) == 0

    gc.collect()
    survivors = sum(1 for r in refs if r() is not None)
    assert survivors == 0, f"{survivors} of {4 * n_per_thread} expressions survived being dropped"


# =============================================================================
# Stress: many threads, many distinct expressions, many evaluations each
# (scaled-down port of MemoryLeakStressTest.java)
# =============================================================================


def _run_stress_thread(thread_id: int, cycles: int, evals_per_expression: int, items_count: int) -> None:
    factory = jsonata.JsonataExpressionFactory()
    rng = random.Random(thread_id * 31)

    for cycle in range(cycles):
        # Globally unique id across all threads -- used as both the tag
        # string and the regex pattern, so every compiled expression here
        # is distinct (nothing is served from a shared cache).
        uid = thread_id * cycles + cycle
        tag = f"t{uid}"
        mult = (uid % 9) + 1  # 1..9

        expr_text = (
            "$map("
            f"$filter(items, function($v){{ $contains($v.tag, /{tag}/) }}),"
            f" function($v){{ $v.num * {mult} }})"
        )
        expression = factory.compile(expr_text)

        for _ in range(evals_per_expression):
            matching_nums = []
            items = []
            for i in range(items_count):
                matches = rng.random() < 0.5
                num = rng.randint(1, 100)
                items.append({"tag": tag if matches else f"x{i}", "num": num})
                if matches:
                    matching_nums.append(num)

            result = expression.evaluate({"items": items})
            _assert_stress_result(result, matching_nums, mult, expr_text)


def _assert_stress_result(result: object, matching_nums: list[int], mult: int, expr_text: str) -> None:
    from jsonata2py.runtime.values import MISSING

    if not matching_nums:
        assert result is MISSING, f"expected undefined for zero matches, got: {result!r} in: {expr_text}"
        return
    if len(matching_nums) == 1:
        assert result == matching_nums[0] * mult, f"single-match value mismatch in: {expr_text}"
        return
    assert isinstance(result, list), f"expected array for {len(matching_nums)} matches, got: {result!r} in: {expr_text}"
    assert result == [n * mult for n in matching_nums], f"array mismatch in: {expr_text}"


def test_unique_expression_stress():
    threads = 4
    cycles_per_thread = 40
    evals_per_expression = 5
    items_count = 10

    with concurrent.futures.ThreadPoolExecutor(max_workers=threads) as pool:
        futures = [
            pool.submit(_run_stress_thread, t, cycles_per_thread, evals_per_expression, items_count)
            for t in range(threads)
        ]
        for f in futures:
            f.result()  # re-raises if a thread failed an assertion
