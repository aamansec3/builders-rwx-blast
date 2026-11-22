"""Label patches by running sqlglot's whole suite on each, like the scorer did.

usage: groundtruth.py SQLGLOT_CHECKOUT VENV_PYTHON SET_DIR TESTS_JSON
SET_DIR holds commits/*.patch; answers.json is written (and resumed) there.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))


def main(root, python, set_dir, tests_json):
    tests = json.load(open(tests_json))["tests"]
    num = {t["id"]: t["number"] for t in tests}
    ans_path = os.path.join(set_dir, "answers.json")
    answers = json.load(open(ans_path))["commits"] if os.path.exists(ans_path) else {}
    bindir = tempfile.mkdtemp()
    os.symlink(python, os.path.join(bindir, "python"))
    for fn in sorted(os.listdir(os.path.join(set_dir, "commits"))):
        cid = fn[:-6]
        if cid in answers:
            continue
        work = tempfile.mkdtemp()
        for d in ("sqlglot", "tests", "sqlglotc"):
            shutil.copytree(os.path.join(root, d), os.path.join(work, d), symlinks=True)
        for f in ("pyproject.toml", "setup.cfg", "setup.py"):
            shutil.copy(os.path.join(root, f), work)
        patch = os.path.abspath(os.path.join(set_dir, "commits", fn))
        subprocess.run(["git", "apply", patch], cwd=work, check=True)
        results = os.path.join(work, "results.txt")
        env = dict(os.environ, BLAST_RESULTS=results, BLAST_INLINE_POOL="0", SKIP_INTEGRATION="0",
                   PYTHONPATH=f"{work}:{os.path.join(HERE, '..', 'selector')}",
                   PATH=f"{bindir}:{os.environ['PATH']}", PYTHONDONTWRITEBYTECODE="1")
        env["BLAST_TIMEOUTS"] = os.path.abspath(tests_json)
        try:
            subprocess.run([python, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "runner_plugin",
                            "-n", "6", "tests"], cwd=work, env=env, timeout=480,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except subprocess.TimeoutExpired:
            subprocess.run(["pkill", "-f", work])
            answers[cid] = {"failing": [], "kind": "aborted"}
            json.dump({"commits": answers}, open(ans_path, "w"), indent=1)
            print(cid, "aborted", flush=True)
            shutil.rmtree(work, ignore_errors=True)
            continue
        failing, errors = set(), []
        for line in open(results):
            st, _, nid = line.rstrip("\n").split(" ", 2)
            if st == "fail" and nid in num:
                failing.add(num[nid])
            elif st == "collect_error":
                errors.append(nid)
        for t in tests:
            if any(t["file"].startswith(e) for e in errors if e):
                failing.add(t["number"])
        answers[cid] = {"failing": sorted(failing), "kind": "code"}
        json.dump({"commits": answers}, open(ans_path, "w"), indent=1)
        print(cid, len(failing), flush=True)
        shutil.rmtree(work)


if __name__ == "__main__":
    main(*sys.argv[1:])
