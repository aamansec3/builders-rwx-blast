"""Fold trace_plugin output into the test map the selector loads.

usage: build_map.py OUT.json.gz TRACE_DIR...

Every test gets the code, classes and files it touched in its own phases,
plus what its class's setup touched in any test (setUpClass runs once, inside
whichever test comes first) and what its module touched at import.
"""

import collections
import glob
import gzip
import json
import sys


def main(out, *trace_dirs):
    fields = ("codes", "types", "files", "lines")
    per_test = collections.defaultdict(lambda: {k: set() for k in fields})
    setup_by_class = collections.defaultdict(lambda: {k: set() for k in fields})
    by_module = collections.defaultdict(lambda: {k: set() for k in fields})
    for path in [p for d in trace_dirs for p in glob.glob(f"{d}/*")]:
        for line in open(path):
            rec = json.loads(line)
            vals = {
                "codes": {f"{f}:{n}" for f, n in rec["codes"]},
                "types": set(rec["types"]),
                "files": set(rec["files"]),
                "lines": {f"{f}:{n}" for f, n in rec.get("lines", ())},
            }
            if rec["kind"] == "collect":
                target = by_module[rec["file"]]
            else:
                target = per_test[rec["id"]]
            for k in fields:
                target[k] |= vals[k]
            if rec["kind"] == "test" and rec["phase"] != "call":
                cls = rec["id"].rsplit("::", 1)[0]
                for k in fields:
                    setup_by_class[cls][k] |= vals[k]

    ids = sorted(per_test)
    index = {"codes": collections.defaultdict(list), "types": collections.defaultdict(list),
             "files": collections.defaultdict(list)}
    for i, tid in enumerate(ids):
        cls = tid.rsplit("::", 1)[0]
        module = tid.split("::", 1)[0]
        for key in index:
            vals = per_test[tid][key] | setup_by_class[cls][key] | by_module[module][key]
            for v in vals:
                index[key][v].append(i)
    lines = collections.defaultdict(list)
    for i, tid in enumerate(ids):
        cls = tid.rsplit("::", 1)[0]
        module = tid.split("::", 1)[0]
        for v in per_test[tid]["lines"] | setup_by_class[cls]["lines"] | by_module[module]["lines"]:
            lines[v].append(i)
    # many lines share a test set (one basic block): store each set once
    sets: dict[tuple, int] = {}
    by_file: dict[str, dict[str, int]] = collections.defaultdict(dict)
    for key, tests in lines.items():
        f, n = key.rsplit(":", 1)
        by_file[f][n] = sets.setdefault(tuple(tests), len(sets))
    data = {"tests": ids, **{k: dict(v) for k, v in index.items()},
            "line_sets": [list(t) for t in sets], "lines": by_file}
    with gzip.open(out, "wt") as f:
        json.dump(data, f, separators=(",", ":"))
    print("line sets", len(sets), "lines", len(lines), file=sys.stderr)
    print(len(ids), {k: len(v) for k, v in index.items()}, file=sys.stderr)


if __name__ == "__main__":
    main(*sys.argv[1:])
