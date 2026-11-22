"""Make synthetic single-edit patches against the pinned sqlglot, sample-style.

usage: mutate.py SQLGLOT_CHECKOUT OUT_DIR COUNT SEED
"""

import ast
import difflib
import os
import random
import re
import sys

SWAPS = [
    (r" == ", " != "), (r" != ", " == "), (r" < ", " <= "), (r" <= ", " < "),
    (r" > ", " >= "), (r" >= ", " > "), (r" is not ", " != "), (r" is ", " == "),
    (r" and ", " or "), (r" or ", " and "), (r" not in ", " in "), (r" in ", " not in "),
    (r"\bif not ", "if "), (r"\bif ", "if not "), (r"\bTrue\b", "False"), (r"\bFalse\b", "True"),
    (r"\bNone\b", "0"), (r"\breturn ", "return not "),
]
COMMENTS = ["# Cheap check first", "# Keep in sync with the generator", "# Intentionally not cached",
            "# Mirrors the behavior of the reference implementation", "# Kept for backwards compatibility"]


def mutate_line(line, rng):
    opts = []
    for pat, rep in SWAPS:
        for m in re.finditer(pat, line):
            opts.append((m.start(), m.end(), rep))
    for m in re.finditer(r"(?<![\w.])(\d+)(?![\w.])", line):
        opts.append((m.start(), m.end(), str(int(m.group(1)) + rng.choice([1, -1]))))
    for m in re.finditer(r'"([A-Z_]{2,})"', line):
        opts.append((m.start(), m.end(), f'"{m.group(1)}X"'))
    if not opts:
        return None
    s, e, rep = rng.choice(opts)
    return line[:s] + rep + line[e:]


def main(root, out, count, seed):
    rng = random.Random(int(seed))
    files = []
    for d, _, fs in os.walk(os.path.join(root, "sqlglot")):
        for f in fs:
            if f.endswith(".py") and "__pycache__" not in d:
                files.append(os.path.relpath(os.path.join(d, f), root))
    files.sort()
    weights = [os.path.getsize(os.path.join(root, f)) ** 0.5 for f in files]
    os.makedirs(out, exist_ok=True)
    made = 0
    while made < int(count):
        rel = rng.choices(files, weights)[0]
        src = open(os.path.join(root, rel)).read()
        lines = src.split("\n")
        tree = ast.parse(src)
        i = rng.randrange(len(lines))
        line = lines[i]
        if not line.strip() or line.strip().startswith("#"):
            continue
        new_lines = list(lines)
        if rng.random() < 0.12:
            indent = re.match(r"\s*", line).group(0)
            new_lines.insert(i, indent + rng.choice(COMMENTS))
        else:
            new = mutate_line(line, rng)
            if new is None or new == line:
                continue
            new_lines[i] = new
            try:
                if ast.dump(ast.parse("\n".join(new_lines))) == ast.dump(tree):
                    continue
            except SyntaxError:
                continue
        diff = difflib.unified_diff(lines, new_lines, f"a/{rel}", f"b/{rel}", lineterm="", n=3)
        text = f"diff --git a/{rel} b/{rel}\n" + "\n".join(diff) + "\n"
        name = f"m{seed}_{made:03d}"
        open(os.path.join(out, name + ".patch"), "w").write(text)
        made += 1


if __name__ == "__main__":
    main(*sys.argv[1:])
