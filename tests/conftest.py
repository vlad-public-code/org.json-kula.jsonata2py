"""Shared pytest configuration.

Only registers the option the performance gate needs -- see
tests/benchmarks/test_performance_regression.py for why the baseline is
recorded per machine rather than committed.
"""

from __future__ import annotations


def pytest_addoption(parser):
    parser.addoption(
        "--perf-record",
        action="store_true",
        default=False,
        help="record a performance baseline for the -m perfgate tests on this machine",
    )
