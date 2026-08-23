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
