"""A loose performance regression gate.

§11.2 of the design spec left the benchmark harness measuring without
asserting, which was right while the port was in flight. It is the wrong
default now: several of the optimisations this library depends on --
type-first dispatch in the value model, the specialised sequence loop in
field(), the length-guarded significant-digit check, the codegen that
emits `and`/`or` inline -- are exactly the kind of thing a later refactor
reverts by accident without breaking a single correctness test.

Design notes, because performance gates have a bad reputation and mostly
deserve it:

  * **The baseline is per-machine and is never committed.** Timings are
    not portable, and a baseline recorded on someone else's laptop is
    worse than no baseline. Record one with

        python -m pytest tests/benchmarks -m perfgate --perf-record

    and the gate starts enforcing on that machine. Without a baseline
    file the tests skip, so a fresh clone and CI stay green.

  * **The threshold is deliberately loose** (see _MAX_RATIO). A gate that
    catches a 3x regression is worth far more than one that flakes at
    1.1x on a noisy box and gets disabled within a month.

  * **The statistic is the minimum of several medians.** Scheduler noise,
    GC pauses and thermal effects only ever make a measurement slower, so
    the fastest observation is the least contaminated estimate of the
    real cost.

  * **Opt-in**, like the comparison benchmarks: `-m perfgate`.
"""

from __future__ import annotations

import json
import pathlib
import statistics
import time

import pytest

from jsonata2py.factory import JsonataExpressionFactory

pytestmark = pytest.mark.perfgate

_BASELINE = pathlib.Path(__file__).with_name("perf_baseline.json")

# Fail only on a regression this large. Anything smaller is not
# distinguishable from machine noise without far more sampling than a
# test run should do.
_MAX_RATIO = 1.5

_RESOURCES = pathlib.Path(__file__).resolve().parents[1] / "resources" / "benchmark"
_W1 = (_RESOURCES / "benchmark_expression.jsonata").read_text(encoding="utf-8")
_W1_INPUT = json.loads((_RESOURCES / "benchmark_input.json").read_text(encoding="utf-8"))


def _items(n: int = 2000) -> dict:
    import random

    rnd = random.Random(42)
    levels = ["junior", "mid", "senior", "lead", "staff"]
    return {
        "items": [
            {
                "id": i,
                "name": f"item{i}",
                "price": round(rnd.uniform(1, 500), 2),
                "qty": rnd.randint(1, 20),
                "level": levels[i % len(levels)],
            }
            for i in range(n)
        ]
    }


_W2_INPUT = _items()


def _measure(fn, rounds: int = 15, repeats: int = 5) -> float:
    """Minimum over `repeats` medians of `rounds` timings, in milliseconds."""
    for _ in range(3):
        fn()
    best = None
    for _ in range(repeats):
        samples = []
        for _ in range(rounds):
            start = time.perf_counter()
            fn()
            samples.append((time.perf_counter() - start) * 1000.0)
        median = statistics.median(samples)
        if best is None or median < best:
            best = median
    assert best is not None
    return best


def _evaluator(expr: str, data):
    compiled = JsonataExpressionFactory().compile(expr)
    compiled.evaluate(data)
    return lambda: compiled.evaluate(data)


def _cold_compile(expr: str):
    counter = iter(range(1, 1 << 30))

    def run():
        # A fresh factory and unique text defeat both cache tiers, so this
        # really is the pipeline cost and not a cache lookup.
        JsonataExpressionFactory().compile(f"{expr}/*{next(counter)}*/")

    return run


def _workloads() -> dict:
    return {
        "eval_w1": _evaluator(_W1, _W1_INPUT),
        "eval_path_filter": _evaluator("items[price > 250].name", _W2_INPUT),
        "eval_sort_comparator": _evaluator(
            "$sort(items, function($a,$b){ $a.price > $b.price }).name", _W2_INPUT
        ),
        "eval_string_build": _evaluator(
            "$join($map(items, function($i){ $i.name & ':' & $string($i.price) }), ', ')", _W2_INPUT
        ),
        "eval_group_by": _evaluator("items{ level: $sum($.price) }", _W2_INPUT),
        "compile_cold_w1": _cold_compile(_W1),
        "compile_cold_small": _cold_compile("a.b.c"),
    }


@pytest.fixture(scope="module")
def baseline(request):
    if request.config.getoption("--perf-record", default=False):
        recorded = {name: _measure(fn) for name, fn in _workloads().items()}
        _BASELINE.write_text(json.dumps(recorded, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        pytest.skip(f"recorded baseline for {len(recorded)} workloads -> {_BASELINE}")
    if not _BASELINE.exists():
        pytest.skip(
            f"no baseline at {_BASELINE}; record one on this machine with "
            "`pytest tests/benchmarks -m perfgate --perf-record`"
        )
    return json.loads(_BASELINE.read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", sorted(_workloads()))
def test_workload_has_not_regressed(name, baseline):
    if name not in baseline:
        pytest.skip(f"{name} is not in the recorded baseline; re-record to include it")
    expected = baseline[name]
    actual = _measure(_workloads()[name])
    ratio = actual / expected
    assert ratio <= _MAX_RATIO, (
        f"{name} regressed: {actual:.4f} ms vs baseline {expected:.4f} ms ({ratio:.2f}x, "
        f"limit {_MAX_RATIO}x). If this is an intentional trade-off, re-record the baseline."
    )
