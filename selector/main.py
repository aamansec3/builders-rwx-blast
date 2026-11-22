"""Blast radius selector: which sqlglot tests each patch needs, split over workers.

For every patch:
  1. analyze.candidates() finds the tests that can notice the change, from
     the per-test trace map built at the pinned commit.
  2. Cheap candidates are run here, against the patched source, and only the
     ones that fail are kept. Expensive candidates go out unverified, since
     listing a test costs at most its runtime while running it costs more.
  3. What is left is packed onto the cheapest number of workers.

Logs go to stderr; stdout carries only the answer lines.
"""

from __future__ import annotations

import heapq
import json
import os
import shutil
import signal
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from analyze import Index, candidates, neighbors, parse_patch  # noqa: E402

DATA = os.environ.get("BLAST_DATA", "/data")
SRC = os.environ.get("BLAST_SRC", "/opt/sqlglot")
MAPS = os.environ.get("BLAST_MAPS", os.path.join(HERE, "..", "maps"))
WORK = os.environ.get("BLAST_WORK", "/tmp/blast")
WORKER_MS = 2000
MAX_WORKERS = 8

# Overhead of a pytest child, in CPU seconds, before any test runs: pytest
# start, importing sqlglot and the test modules, class fixtures.
BASE_COST = 1.2
FILE_COST = {
    "tests/test_executor.py": 6.0,  # setUpClass loads the TPC-H and TPC-DS tables
    "tests/test_optimizer.py": 1.5,
}
DEFAULT_FILE_COST = 0.25
# Real CPU in the container over maps/runcost.json, calibrated against
# `make sample` (2haz's candidates: 11 s estimated, ~20-28 s measured).
RUN_SCALE = 1.9
# A candidate whose own cost is above this always goes out unverified.
MAX_RUN_TEST = 3.0 * RUN_SCALE
# Tests recorded at this many seconds or more (tpcds, max_nodes,
# generator_memory) are the only ones the gate may drop unrun.
GIANT_SECONDS = 5.0
# Budget of CPU seconds for local runs in one commit.
MAX_RUN_COMMIT = 12.0
# Most CPU to spend running every cheap candidate in order to gate the
# expensive ones (see plan()).
GATE_MAX_RUN = 25.0
# When a commit lists anything, its cheap neighbors (tests sharing state with
# the edited code, see analyze.neighbors) up to this many seconds each go too.
NEIGHBOR_MAX_SECONDS = 0.5
# Chance that something among a change's cheap candidates fails.
P_FAIL_CODE = 0.45
P_FAIL_DATA = 0.95


def log(*args):
    print(*args, file=sys.stderr, flush=True)


def pack(nums: list[int], ms: dict[int, int]) -> tuple[int, list[list[int]]]:
    """Cheapest split of tests over 1..8 workers: (cost in ms, workers).

    Longest test first onto the least loaded worker, for each worker count.
    """
    if not nums:
        return 0, []
    order = sorted(nums, key=lambda n: -ms[n])
    total = sum(ms[n] for n in order)
    best = None
    for k in range(1, min(MAX_WORKERS, len(order)) + 1):
        # no split can beat the longest test or an even share
        if best is not None and max(ms[order[0]], total // k) + WORKER_MS * k >= best[0]:
            continue
        heap = [(0, i) for i in range(k)]
        workers: list[list[int]] = [[] for _ in range(k)]
        for n in order:
            load, i = heap[0]
            workers[i].append(n)
            heapq.heapreplace(heap, (load + ms[n], i))
        cost = max(load for load, _ in heap) + WORKER_MS * k
        if best is None or cost < best[0]:
            best = (cost, workers)
    return best


def plan(cands: set[int], tests: list[dict], ms: dict[int, int], runcost: dict[int, float], p_fail: float,
         nb: set[int] = frozenset()):
    """Decide what to run here: (estimated cost, run here, list as is, list only on a failure).

    Running a set R here costs its CPU. If nothing in R fails the answer
    drops R; if something does (probability p_fail), assume the worst, that
    the answer keeps everything.

    The gate: when every cheap candidate runs here and passes, the giant ones
    (tpcds, max_nodes, generator_memory) are dropped as well. They are
    broad workloads over code the cheap fixture tests also exercise, and in
    every labeled commit where one failed, several cheap candidates failed
    too. With --maxfail=1 a failing run stops early, at about half its cost.
    """
    # whenever anything is listed, the neighbors nb go along (see main())
    def listed(rest):
        return pack(sorted(set(rest) | nb), ms)[0] / 1000 if rest else 0.0

    full_cost = listed(cands)
    cheap = sorted((n for n in cands if runcost[n] <= MAX_RUN_TEST), key=lambda n: runcost[n])
    best = (full_cost, [], sorted(cands), [])
    run: list[int] = []
    files: set[str] = set()
    spent = BASE_COST
    checkpoint = 1
    for i, n in enumerate(cheap):
        f = tests[n]["file"]
        add = runcost[n] + (0 if f in files else FILE_COST.get(f, DEFAULT_FILE_COST))
        if spent + add > max(MAX_RUN_COMMIT, GATE_MAX_RUN):
            break
        spent += add
        files.add(f)
        run.append(n)
        last = i + 1 == len(cheap)
        if spent <= MAX_RUN_COMMIT:
            # packing is the slow part: weigh the split at 1, 2, 4, ... tests and at the end
            near_cap = last or spent + runcost[cheap[i + 1]] > MAX_RUN_COMMIT
            if len(run) >= checkpoint or near_cap:
                checkpoint *= 2
                rest = sorted(cands - set(run))
                est = spent + (1 - p_fail) * listed(rest) + p_fail * full_cost
                if est < best[0]:
                    best = (est, list(run), rest, [])
        if last:
            unrun = cands - set(run)
            gated = sorted(n for n in unrun if tests[n]["seconds"] >= GIANT_SECONDS)
            rest = sorted(n for n in unrun if tests[n]["seconds"] < GIANT_SECONDS)
            est = (1 - p_fail) * (spent + listed(rest)) + p_fail * (spent / 2 + full_cost)
            if gated and est < best[0]:
                best = (est, list(run), rest, gated)
    return best


def fail_prior(change) -> float:
    """How likely the tests a change reaches are to fail, by kind of change."""
    code = change.funcs or change.lines or change.classes or change.modules or change.broken or change.tests_files
    if change.broken:
        return 1.0
    data = [f for f in change.files if not f[0].endswith(".py")]
    if data and not code:
        return P_FAIL_DATA
    return P_FAIL_CODE


class Overlay:
    """The pinned source as a farm of symlinks, with a patch's files written in."""

    def __init__(self, src: str):
        self.src = src
        self.dirs: list[str] = []
        self.files: list[str] = []
        for top in ("sqlglot", "tests", "sqlglotc"):
            for dirpath, dirnames, filenames in os.walk(os.path.join(src, top)):
                rel = os.path.relpath(dirpath, src)
                self.dirs.append(rel)
                for fn in filenames:
                    self.files.append(os.path.join(rel, fn))
        for fn in ("pyproject.toml", "setup.cfg"):
            if os.path.exists(os.path.join(src, fn)):
                self.files.append(fn)

    def build(self, dest: str, sources: dict[str, str | None]):
        for d in self.dirs:
            os.makedirs(os.path.join(dest, d), exist_ok=True)
        stale_pyc = set()
        for rel in sources:
            if rel.endswith(".py"):
                d, base = os.path.split(rel)
                stale_pyc.add((d, base[:-3] + "."))
        for rel in self.files:
            if rel in sources:
                continue
            d, base = os.path.split(rel)
            if os.path.basename(d) == "__pycache__":
                parent = os.path.dirname(d)
                if any(parent == sd and base.startswith(prefix) for sd, prefix in stale_pyc):
                    continue
            os.symlink(os.path.join(self.src, rel), os.path.join(dest, rel))
        for rel, text in sources.items():
            if text is None:
                continue
            path = os.path.join(dest, rel)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)


def child_run(root: str, nodeids: list[str], results: str, timeouts: float, cpu_limit: int):
    """In a forked child: run the given tests under pytest in the overlay.

    It stops at the first failure: from then on the commit needs workers
    anyway, and listing the untried cheap tests costs less than trying them.
    A patch that makes code spin is cut off by the per-test timeout and,
    failing that, by a CPU limit on the whole child.
    """
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (cpu_limit, cpu_limit + 2))
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    os.chdir(root)
    os.environ["BLAST_RESULTS"] = results
    os.environ["PYTHONPATH"] = root
    os.environ["SKIP_INTEGRATION"] = "0"
    sys.path[:] = [root, HERE] + [p for p in sys.path if p and not p.startswith(SRC)]
    for name in list(sys.modules):
        if name == "sqlglot" or name.startswith("sqlglot.") or name == "tests" or name.startswith("tests."):
            del sys.modules[name]
    import pytest

    args = [
        "-q", "-p", "no:cacheprovider", "-p", "runner_plugin", "-p", "no:xdist",
        "-p", "no:randomly", "--assert=plain", "-o", "addopts=", "--rootdir", root,
        f"--timeout={timeouts:.0f}", "--maxfail=1", *nodeids,
    ]
    code = pytest.main(args)
    os._exit(0 if code in (0, 1) else 3)


def main():
    t0 = time.time()
    tests = json.loads(open(os.path.join(DATA, "tests.json")).read())["tests"]
    ms = {t["number"]: round(t["seconds"] * 1000) for t in tests}
    try:
        with open(os.path.join(MAPS, "runcost.json")) as f:
            measured = json.load(f)
    except OSError:
        measured = {}
    runcost = {t["number"]: measured.get(t["id"], t["seconds"] * 2) * RUN_SCALE for t in tests}
    index = Index(MAPS, tests)
    by_id = {t["id"]: t["number"] for t in tests}

    patches = sorted(f for f in os.listdir(os.path.join(DATA, "commits")) if f.endswith(".patch"))
    jobs = []
    answers: dict[str, list[int]] = {}
    extra: dict[str, set[int]] = {}
    for fn in patches:
        cid = fn[:-6]
        sources: dict[str, str | None] = {}
        try:
            with open(os.path.join(DATA, "commits", fn), encoding="utf-8", errors="replace") as f:
                diffs = parse_patch(f.read())
            cands, change = candidates(diffs, SRC, index, sources)
            cands |= set(index.unmapped)
        except Exception as e:  # noqa: BLE001
            log(f"{cid}: analysis failed ({e!r}); listing every test")
            answers[cid] = [t["number"] for t in tests]
            continue
        extra[cid] = {n for n in neighbors(index, change) - cands if tests[n]["seconds"] <= NEIGHBOR_MAX_SECONDS}
        if any(not rel.startswith(("sqlglot/", "tests/")) for rel in sources):
            # tests may reach files outside sqlglot/ and tests/ through
            # resolved paths, which would bypass the patched copy
            est, run, rest, gated = pack(sorted(cands), ms)[0] / 1000, [], sorted(cands), []
        else:
            est, run, rest, gated = plan(cands, tests, ms, runcost, fail_prior(change), extra[cid])
        log(f"{cid}: {len(cands)} candidates, run {len(run)} here, list {len(rest)}, "
            f"gate {len(gated)}, est {est:.1f}s {change.notes}")
        answers[cid] = rest
        if run:
            jobs.append((cid, run, sources, gated))
    log(f"analysis done in {time.time() - t0:.2f}s, {len(jobs)} commits to run")

    if jobs:
        run_jobs(jobs, tests, by_id, answers, runcost)

    for fn in patches:
        cid = fn[:-6]
        chosen = set(answers[cid])
        if chosen:
            chosen |= extra.get(cid, set())
        cost, workers = pack(sorted(chosen), ms)
        print(json.dumps({"commit": cid, "workers": workers}), flush=True)
    import resource

    cpu = sum(
        u.ru_utime + u.ru_stime
        for u in (resource.getrusage(resource.RUSAGE_SELF), resource.getrusage(resource.RUSAGE_CHILDREN))
    )
    log(f"done in {time.time() - t0:.2f}s wall, {cpu:.2f}s cpu (self and children)")


def run_jobs(jobs, tests, by_id, answers, runcost):
    os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
    os.environ.setdefault("OMP_NUM_THREADS", "1")
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    sys.dont_write_bytecode = True
    # Warm the heavy third-party imports once; every child inherits them.
    for mod in ("pytest", "_pytest.unittest", "_pytest.python", "pytest_timeout", "duckdb", "numpy",
                "pandas", "pandas.testing", "runner_plugin_deps"):
        try:
            __import__(mod)
        except Exception:  # noqa: BLE001
            pass
    overlay = Overlay(SRC)
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK, exist_ok=True)
    parallel = int(os.environ.get("BLAST_PARALLEL", "2"))
    pending = list(jobs)
    running: dict[int, tuple] = {}
    deadline_total = time.time() + float(os.environ.get("BLAST_BUDGET", "150"))

    def start(job):
        cid, run, sources, gated = job
        root = os.path.join(WORK, cid)
        overlay.build(root, sources)
        results = os.path.join(WORK, f"{cid}.results")
        # keep each class together, or pytest re-runs its setUpClass at every switch
        first = {}
        for i, n in enumerate(run):
            first.setdefault(tests[n]["id"].rsplit("::", 1)[0], i)
        nodeids = [tests[n]["id"] for n in sorted(run, key=lambda n: first[tests[n]["id"].rsplit("::", 1)[0]])]
        # the first test of a class also pays for its setUpClass
        setup = sum(FILE_COST.get(f, DEFAULT_FILE_COST) for f in {tests[n]["file"] for n in run})
        limit = 3 * max(runcost[n] for n in run) + 3 * max(FILE_COST.values()) + 5
        cpu_limit = int(6 + 2 * (sum(runcost[n] for n in run) + setup))
        pid = os.fork()
        if pid == 0:
            try:
                child_run(root, nodeids, results, limit, cpu_limit)
            finally:
                os._exit(3)
        budget = 10 + 3 * cpu_limit
        running[pid] = (job, root, results, time.time() + budget)

    def finish(pid, status):
        (cid, run, sources, gated), root, results, _ = running.pop(pid)
        outcome: dict[int, str] = {}
        collect_errors: list[str] = []
        try:
            with open(results) as f:
                for line in f:
                    st, _, nodeid = line.rstrip("\n").split(" ", 2)
                    if st == "collect_error":
                        collect_errors.append(nodeid)
                    elif nodeid in by_id:
                        outcome[by_id[nodeid]] = st
        except OSError:
            pass
        ok = os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0
        failed = []
        for n in run:
            st = outcome.get(n)
            file = tests[n]["file"]
            if st == "fail" or (st is None) or any(file.startswith(e) or not e for e in collect_errors):
                failed.append(n)
        if not ok:
            log(f"{cid}: pytest child ended badly ({status}); keeping unfinished tests")
        log(f"{cid}: ran {len(run)}, {len(failed)} failed")
        answers[cid] = sorted(set(answers[cid]) | set(failed) | (set(gated) if failed else set()))
        shutil.rmtree(root, ignore_errors=True)

    while pending or running:
        while pending and len(running) < parallel:
            job = pending.pop(0)
            if time.time() > deadline_total:
                cid, run, _, gated = job
                log(f"{cid}: out of time; listing its tests unverified")
                answers[cid] = sorted(set(answers[cid]) | set(run) | set(gated))
                continue
            start(job)
        if not running:
            break
        pid, status, usage = os.wait4(-1, os.WNOHANG)
        if pid == 0:
            now = time.time()
            for p, (_, _, _, dl) in list(running.items()):
                if now > dl:
                    log(f"pid {p}: over its time; killing")
                    os.kill(p, signal.SIGKILL)
            time.sleep(0.02)
            continue
        if pid in running:
            log(f"{running[pid][0][0]}: child cpu {usage.ru_utime + usage.ru_stime:.2f}s")
            finish(pid, status)


if __name__ == "__main__":
    main()
