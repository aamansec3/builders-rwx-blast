# Blast Radius selector

For each patch to sqlglot (pinned at `eb11aec4`), pick the tests that can fail and split them over up to 8 workers.

## How it decides

1. **Trace map, built once at the pinned commit** (`maps/testmap.json.gz`).
   Every test was run under `sys.monitoring` and recorded:
   - each sqlglot function (code object) it started,
   - each sqlglot class it instantiated,
   - each repository file it opened or listed (fixtures).

   Two passes are merged: a normal run, and a run where every test runs in a fresh fork, so code that one test warms up (caches, class fixtures) is charged to every test that needs it. Process pools run inline during tracing, with a pickle round trip, so work done in pool workers is still traced.

2. **Static index** (`maps/static.json.gz`): class hierarchy, which code mentions each name, which modules import each module, which classes hand out other classes (a dialect's `Generator`), and the code that runs at import time.

3. **Patch analysis** (`selector/analyze.py`). The patch is applied in memory. For each edited Python file, only the syntax-tree nodes around each edit are compared, so comment-only and formatting edits drop out. An edit is then
   - inside a function or lambda: the tests that ran that function;
   - a class attribute: the tests that used the class (or a subclass, its enclosing class, or a class that hands it out), narrowed to the tests that ran code reading that attribute, unless the attribute is read at import time;
   - another class-body change (methods added or removed): every test that used the class;
   - a module-level statement: the tests that ran code naming what it binds, and the classes and modules built from it. Imports, or anything opaque, fall back to every test using the module or a module that imports it.

   Fixtures and other files map to the tests that opened them. Edited test files map to their changed tests.

4. **Run or list** (`selector/main.py`). Listing a test costs at most its recorded time; running it here costs about that much in CPU plus pytest start-up. So cheap candidates are run against the patched source when the expected saving pays for it (the chance that something fails depends on the kind of change), and the rest are listed unverified. Local runs use a symlink copy of sqlglot in `/tmp` with the patched files written in, forked from a parent that has pytest, duckdb and pandas loaded, with process pools inline, a per-test timeout, `--maxfail=1` and a CPU limit. Any test that does not finish cleanly counts as failing.

5. **Packing**: longest test first onto the least loaded worker, for each worker count from 1 to 8, keeping the cheapest.

## Rebuilding the maps

Needs a sqlglot checkout at the pinned commit and a Python 3.13 environment with `requirements.lock` plus `coverage`:

```sh
export SKIP_INTEGRATION=0 PYTHONPATH=$SQLGLOT:$PWD/tools BLAST_ROOT=$SQLGLOT
(cd $SQLGLOT && BLAST_TRACE_OUT=/tmp/trace/t python -m pytest -p trace_plugin -n 7 tests)
(cd $SQLGLOT && BLAST_FORK=1 BLAST_TRACE_OUT=/tmp/trace_fork/t python -m pytest -p trace_plugin -n 7 tests)
python tools/build_map.py maps/testmap.json.gz /tmp/trace /tmp/trace_fork
python tools/import_codes.py $SQLGLOT /tmp/import_codes.json
python tools/static_index.py $SQLGLOT maps/static.json.gz /tmp/import_codes.json
```

`tools/mutate.py` and `tools/groundtruth.py` make and label synthetic patches for checking recall; `tools/eval_static.py` and `tools/eval_out.py` score candidate sets and outputs against labels.

## Try it

```sh
make sample   # build, run on the 30 sample commits under the scorer's limits, score
```
