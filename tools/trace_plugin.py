"""pytest plugin that records, per test, what sqlglot code and files it touches.

Loaded with `-p trace_plugin` while running sqlglot's suite at the pinned
commit. Each pytest process appends JSON lines to $BLAST_TRACE_OUT.<pid>:

  {"kind": "test", "id": nodeid, "phase": "setup"|"call"|"teardown",
   "codes": [[relfile, firstlineno], ...], "types": [...], "files": [...]}
  {"kind": "collect", "file": relfile, "codes": ..., "types": ..., "files": ...}

codes: every sqlglot code object that started running (function granularity).
types: sqlglot classes instantiated (via wrapped __init__ methods).
files: repo files opened or listed (fixtures and the like).
lines: with BLAST_LINES=1, every sqlglot (file, line) that ran.
"""

import importlib
import json
import os
import pkgutil
import sys

ROOT = os.path.realpath(os.environ["BLAST_ROOT"]) + os.sep
OUT = open(f"{os.environ['BLAST_TRACE_OUT']}.{os.getpid()}", "a")
PKG = ROOT + "sqlglot" + os.sep

mon = sys.monitoring
TOOL = 4
state = {"codes": set(), "types": set(), "files": set(), "lines": set(), "on": False}
LINES = os.environ.get("BLAST_LINES") == "1"


def _py_start(code, offset):
    fn = code.co_filename
    if state["on"] and fn.startswith(PKG):
        state["codes"].add((fn[len(ROOT):], code.co_firstlineno))
    return mon.DISABLE


def _line(code, line):
    fn = code.co_filename
    if state["on"] and fn.startswith(PKG):
        state["lines"].add((fn[len(ROOT):], line))
    return mon.DISABLE


def _audit(event, args):
    try:
        _audit_inner(event, args)
    except Exception:  # noqa: BLE001  (globals vanish at interpreter shutdown)
        pass


def _audit_inner(event, args):
    if not state["on"]:
        return
    if event not in ("open", "os.listdir", "os.scandir", "glob.glob") or not args:
        return
    path = args[0]
    if isinstance(path, bytes):
        path = path.decode(errors="replace")
    if not isinstance(path, str):
        return
    path = os.path.realpath(path)
    if path.startswith(ROOT):
        state["files"].add(path[len(ROOT):])


def _start():
    state["codes"] = set()
    state["types"] = set()
    state["files"] = set()
    state["lines"] = set()
    state["on"] = True
    mon.restart_events()


def _stop():
    state["on"] = False
    return {
        "codes": sorted(state["codes"]),
        "types": sorted(state["types"]),
        "files": sorted(state["files"]),
        "lines": sorted(state["lines"]),
    }


def _wrap_init(cls, init):
    def __init__(self, *args, **kwargs):
        if state["on"]:
            t = type(self)
            state["types"].add(f"{t.__module__}:{t.__qualname__}")
        return init(self, *args, **kwargs)

    __init__.__wrapped__ = init
    return __init__


def _preimport():
    """Import every sqlglot module so module and class bodies run outside tests."""
    import sqlglot

    for info in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        if info.name.endswith("__main__"):
            continue
        try:
            importlib.import_module(info.name)
        except Exception as e:  # noqa: BLE001
            print(f"preimport {info.name}: {e}", file=sys.stderr)
    from sqlglot.dialects.dialect import Dialect, Dialects

    for d in Dialects:
        if d.value:
            try:
                Dialect.get_or_raise(d.value)
            except Exception as e:  # noqa: BLE001
                print(f"dialect {d}: {e}", file=sys.stderr)

    seen = set()
    for name, mod in list(sys.modules.items()):
        if not name.startswith("sqlglot") or mod is None:
            continue
        for obj in list(vars(mod).values()):
            if isinstance(obj, type) and obj.__module__ == name:
                _wrap_classes(obj, seen)


def _wrap_classes(cls, seen):
    if cls in seen:
        return
    seen.add(cls)
    init = cls.__dict__.get("__init__")
    if init is not None and hasattr(init, "__code__"):
        try:
            cls.__init__ = _wrap_init(cls, init)
        except (TypeError, AttributeError):
            pass
    for obj in list(vars(cls).values()):
        if isinstance(obj, type) and obj.__module__ == cls.__module__:
            _wrap_classes(obj, seen)


def _emit(rec):
    OUT.write(json.dumps(rec) + "\n")
    OUT.flush()


def _roundtrip(obj):
    """Pickle and unpickle, as crossing into a worker process would."""
    import pickle

    return pickle.loads(pickle.dumps(obj))


class _InlineExecutor:
    """Stands in for ProcessPoolExecutor so pooled work is traced in-process."""

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
    import concurrent.futures

    concurrent.futures.ProcessPoolExecutor = _InlineExecutor
    _preimport()
    mon.use_tool_id(TOOL, "blastmap")
    mon.register_callback(TOOL, mon.events.PY_START, _py_start)
    if LINES:
        mon.register_callback(TOOL, mon.events.LINE, _line)
        mon.set_events(TOOL, mon.events.PY_START | mon.events.LINE)
    else:
        mon.set_events(TOOL, mon.events.PY_START)
    sys.addaudithook(_audit)


def pytest_collectstart(collector):
    if collector.__class__.__name__ == "Module":
        _start()


def pytest_collectreport(report):
    if report.fspath and str(report.fspath).endswith(".py") and "::" not in report.nodeid and state["on"]:
        rec = _stop()
        rec.update(kind="collect", file=report.nodeid)
        _emit(rec)


import pytest  # noqa: E402


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_setup(item):
    _start()
    yield
    rec = _stop()
    rec.update(kind="test", id=item.nodeid, phase="setup")
    _emit(rec)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_call(item):
    _start()
    yield
    rec = _stop()
    rec.update(kind="test", id=item.nodeid, phase="call")
    _emit(rec)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_teardown(item):
    _start()
    yield
    rec = _stop()
    rec.update(kind="test", id=item.nodeid, phase="teardown")
    _emit(rec)


if os.environ.get("BLAST_FORK") == "1":

    @pytest.hookimpl(tryfirst=True)
    def pytest_runtest_protocol(item, nextitem):
        """Run each test in a fork of the post-collection process.

        Nothing one test warms up (caches, class fixtures, lazily built
        tables) is left for the next, so each test is charged with all the
        code it needs from a cold start.
        """
        from _pytest.runner import runtestprotocol

        pid = os.fork()
        if pid == 0:
            try:
                runtestprotocol(item, nextitem=None, log=False)
            finally:
                OUT.flush()
                os._exit(0)
        os.waitpid(pid, 0)
        return True
