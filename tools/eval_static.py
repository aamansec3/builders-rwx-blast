"""Candidate recall and size on the sample commits, without running tests."""
import json, sys, os, glob
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "selector"))
from analyze import Index, parse_patch, candidates

src, sample = sys.argv[1], sys.argv[2]
tests = json.load(open(f"{sample}/tests.json"))["tests"]
ans = json.load(open(f"{sample}/answers.json"))["commits"]
idx = Index(os.path.join(os.path.dirname(__file__), "..", "maps"), tests)
print("unmapped", idx.unmapped)
sec = {t["number"]: t["seconds"] for t in tests}
for p in sorted(glob.glob(f"{sample}/commits/*.patch")):
    cid = os.path.basename(p)[:-6]
    cand, ch = candidates(parse_patch(open(p).read()), src, idx, {})
    fail = set(ans[cid]["failing"])
    miss = fail - cand
    print(f"{cid} {ans[cid]['kind']:8} fail={len(fail):3} cand={len(cand):4} csec={sum(sec[c] for c in cand):7.2f} "
          f"MISS={sorted(miss)} f={len(ch.funcs)}+{len(ch.lines)} c={[c[0] for c in ch.classes]} m={[(m[0], sorted(m[2])) for m in ch.modules]} {ch.notes}")
