"""Score selector output against whatever commits of a set are labeled so far.

usage: eval_out.py SET_DIR OUTPUT.jsonl
"""
import json
import sys

set_dir, out = sys.argv[1:]
tests = {t["number"]: t for t in json.load(open(f"{set_dir}/tests.json"))["tests"]}
ans = json.load(open(f"{set_dir}/answers.json"))["commits"]
total = misses = n = 0
for line in open(out):
    r = json.loads(line)
    if r["commit"] not in ans:
        continue
    n += 1
    w = [x for x in r["workers"] if x]
    cost = (max(sum(round(tests[t]["seconds"] * 1000) for t in x) for x in w) + 2000 * len(w)) / 1000 if w else 0
    chosen = {t for x in w for t in x}
    missed = set(ans[r["commit"]]["failing"]) - chosen
    total += cost
    if missed:
        misses += 1
        total += 300
    print(f"{r['commit']} fail={len(ans[r['commit']]['failing']):4} cost={cost:6.2f} {'MISS ' + str(sorted(missed)[:8]) if missed else ''}")
print(f"labeled {n}: total {total:.1f}s, misses {misses}")
