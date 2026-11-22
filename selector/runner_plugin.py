"""pytest plugin the selector loads when it runs candidate tests itself.

It writes one line per finished test to $BLAST_RESULTS: "<status> <seconds> <nodeid>",
status being "fail" or "pass". A test fails on any failed report, subtests
included. A collection error fails every requested test in that file.

Process pools run inline (with a pickle round trip, so arguments still cross
a serialization boundary): spawning workers that each import sqlglot would
cost more CPU than the tests themselves.
"""

import os
import pickle
import time

import pytest

_out = open(os.environ["BLAST_RESULTS"], "a", buffering=1)
_failed: set[str] = set()
_started: dict[str, float] = {}


def _roundtrip(obj):
    return pickle.loads(pickle.dumps(obj))


class InlineExecutor:
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        if initializer is not None:
            initializer(*_roundtrip(initargs))

    def submit(self, fn, /, *args, **kwargs):
        from concurrent.futures import Future

        fut = Future()
        try:
            args, kwargs = _roundtrip((args, kwargs))
            fut.set_result(_roundtrip(fn(*args, **kwargs)))
        except BaseException as e:  # noqa: BLE001
            fut.set_exception(e)
        return fut

    def map(self, fn, *iterables, **kw):
        return [_roundtrip(fn(*_roundtrip(args))) for args in zip(*iterables)]

    def shutdown(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def pytest_configure(config):
    if os.environ.get("BLAST_INLINE_POOL", "1") == "1":
        import concurrent.futures

        concurrent.futures.ProcessPoolExecutor = InlineExecutor


def pytest_collection_modifyitems(items):
    """With $BLAST_TIMEOUTS (a tests.json path), give each test a timeout
    scaled from its recorded time instead of one flat limit."""
    path = os.environ.get("BLAST_TIMEOUTS")
    if not path:
        return
    import json

    seconds = {t["id"]: t["seconds"] for t in json.load(open(path))["tests"]}
    for item in items:
        limit = max(60.0, 12 * seconds.get(item.nodeid, 10.0))
        item.add_marker(pytest.mark.timeout(limit))


def pytest_collectreport(report):
    if report.failed:
        _out.write(f"collect_error 0 {report.nodeid}\n")


def pytest_runtest_logstart(nodeid, location):
    _started[nodeid] = time.process_time()


def pytest_runtest_logreport(report):
    if report.failed:
        _failed.add(report.nodeid)


def pytest_runtest_logfinish(nodeid, location):
    spent = time.process_time() - _started.pop(nodeid, time.process_time())
    _out.write(f"{'fail' if nodeid in _failed else 'pass'} {spent:.3f} {nodeid}\n")
