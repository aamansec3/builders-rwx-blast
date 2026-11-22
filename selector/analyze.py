"""Work out which tests a patch can reach.

A patch is applied to the pinned sqlglot source in memory, and each changed
Python file is compared with its original as syntax trees, so comments and
blank lines drop out. What changed is then sorted into three kinds:

  function: a def or lambda body changed. The tests that ran that code are
            the ones that can notice.
  class:    a statement in a class body changed (an attribute, a dict of
            transforms, a method added or removed). The tests that built an
            instance of that class or any subclass, or ran its methods.
  module:   a top-level statement changed. The tests that ran code naming
            what it binds, and the classes and modules built from it.

Fixture and other data files map to the tests that opened them.
"""

from __future__ import annotations

import ast
import difflib
import gzip
import json
import os
import re
from bisect import bisect_left, bisect_right
from dataclasses import dataclass, field

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class FileDiff:
    old: str | None
    new: str | None
    hunks: list = field(default_factory=list)


def parse_patch(text: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    cur = None
    lines = text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*) b/(.*)$", line)
            cur = FileDiff(m.group(1), m.group(2))
            files.append(cur)
        elif cur is not None and line.startswith("--- ") and not cur.hunks:
            path = line[4:].split("\t")[0]
            cur.old = None if path == "/dev/null" else path[2:]
        elif cur is not None and line.startswith("+++ ") and not cur.hunks:
            path = line[4:].split("\t")[0]
            cur.new = None if path == "/dev/null" else path[2:]
        elif line.startswith("rename from ") and cur is not None:
            cur.old = line[len("rename from "):]
        elif line.startswith("rename to ") and cur is not None:
            cur.new = line[len("rename to "):]
        elif line.startswith("new file mode") and cur is not None:
            cur.old = None
        elif line.startswith("deleted file mode") and cur is not None:
            cur.new = None
        elif line.startswith("@@") and cur is not None:
            m = HUNK.match(line)
            old_start, old_len = int(m.group(1)), int(m.group(2) or 1)
            new_len = int(m.group(4) or 1)
            body = []
            o = n = 0
            i += 1
            while i < len(lines) and (o < old_len or n < new_len):
                h = lines[i]
                if h.startswith("\\"):
                    i += 1
                    continue
                tag, rest = (h[:1] or " "), h[1:]
                body.append((tag, rest))
                if tag in " -":
                    o += 1
                if tag in " +":
                    n += 1
                i += 1
            while i < len(lines) and lines[i].startswith("\\"):
                if body and "newline" in lines[i]:
                    body[-1] = (body[-1][0], body[-1][1], "nonl")
                i += 1
            cur.hunks.append((old_start, body))
            continue
        i += 1
    return files


def apply_hunks(old_text: str, hunks) -> tuple[str, list]:
    """The patched text, and its edits as (old touches, new touches).

    An edit is a run of removed and added lines. Its touches are the line
    numbers it removes (old) or adds (new); a side with no lines gets the
    half-line where the other side's lines go, e.g. 12.5 for "after line 12".
    """
    old = old_text.split("\n")
    out: list[str] = []
    edits = []
    pos = 0  # index into old (0-based)
    for old_start, body in hunks:
        start = max(old_start - 1, 0)
        if not any(t in " -" for t, *_ in body):
            start = old_start  # pure insertion after line old_start
        out.extend(old[pos:start])
        pos = start
        run_old: list[float] = []
        run_new: list[float] = []
        for tag, rest, *_ in body + [(" ", None)]:
            if tag == " ":
                if run_old or run_new:
                    edits.append((run_old or [pos + 0.5], run_new or [len(out) + 0.5]))
                    run_old, run_new = [], []
                if rest is None:
                    break
                out.append(old[pos])
                pos += 1
            elif tag == "-":
                pos += 1
                run_old.append(pos)
            else:
                out.append(rest)
                run_new.append(len(out))
    out.extend(old[pos:])
    return "\n".join(out), edits


def code_line(node) -> int:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.decorator_list:
        return min([node.lineno] + [d.lineno for d in node.decorator_list])
    return node.lineno


FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def units(tree):
    """Every def and lambda by a stable path, and every class (and the module).

    Returns (funcs, scopes), each mapping a path to its node. A path names
    the enclosing classes and defs, with an occurrence count so that
    redefinitions (property setters, overloads) and lambdas stay distinct.
    """
    funcs: dict = {}
    scopes: dict = {(): tree}

    def walk(node, path, counters):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                name = "<lambda>" if isinstance(child, ast.Lambda) else child.name
                key = (name, counters.setdefault(name, 0))
                counters[name] += 1
                p = path + (key,)
                funcs[p] = child
                walk(child, p, {})
            elif isinstance(child, ast.ClassDef):
                name = "class " + child.name
                key = (name, counters.setdefault(name, 0))
                counters[name] += 1
                p = path + (key,)
                scopes[p] = child
                walk(child, p, {})
            else:
                walk(child, path, counters)

    walk(tree, (), {})
    return funcs, scopes


def text(node, lines) -> str:
    return "\n".join(lines[code_line(node) - 1:node.end_lineno])


def same_node(a, b, a_lines, b_lines) -> bool:
    """Equal as syntax: identical text is, otherwise compare the trees."""
    return text(a, a_lines) == text(b, b_lines) or ast.dump(a) == ast.dump(b)


def skeleton_key(stmt, lines) -> str:
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return f"<def {stmt.name}>"
    if isinstance(stmt, ast.ClassDef):
        return "<class %s %s>" % (stmt.name, ast.dump(ast.Tuple(elts=stmt.bases + stmt.decorator_list + [k.value for k in stmt.keywords])))
    return text(stmt, lines)


def masked_dump(stmt) -> str:
    """A statement's dump with nested defs and lambdas reduced to markers."""

    class Masker(ast.NodeTransformer):
        def visit_FunctionDef(self, node):
            return ast.Name(id=f"<def {node.name}>", ctx=ast.Load())

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node):
            return ast.ClassDef(name=node.name, bases=node.bases, keywords=node.keywords,
                                body=[], decorator_list=node.decorator_list, type_params=[])

        def visit_Lambda(self, node):
            return ast.Name(id="<lambda>", ctx=ast.Load())

    import copy

    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return f"<def {stmt.name}>"
    return ast.dump(Masker().visit(copy.deepcopy(stmt)))


def skeleton_changes(old_body, new_body, old_lines, new_lines):
    """Statements of a class or module body that differ outside nested defs/lambdas."""
    a = [skeleton_key(s, old_lines) for s in old_body]
    b = [skeleton_key(s, new_lines) for s in new_body]
    if a == b:
        return [], []
    removed, added = [], []
    sm = difflib.SequenceMatcher(a=a, b=b, autojunk=False)
    for tag, a0, a1, b0, b1 in sm.get_opcodes():
        if tag == "equal":
            continue
        olds, news = old_body[a0:a1], new_body[b0:b1]
        if [masked_dump(x) for x in olds] == [masked_dump(x) for x in news]:
            continue
        removed += olds
        added += news
    return removed, added


def bound_names(stmt) -> set[str]:
    out = set()
    for n in ast.walk(stmt):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            out.add(n.id)
        elif isinstance(n, ast.alias):
            out.add((n.asname or n.name).split(".")[0])
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.isidentifier():
            # dict keys of class attributes like {"FUNCTIONS": ...}
            pass
    return out


@dataclass
class Change:
    funcs: list = field(default_factory=list)      # (relfile, lo, hi) spans in the old file
    lines: list = field(default_factory=list)      # (relfile, lo, hi, func_lo, func_hi): a statement
    classes: list = field(default_factory=list)    # (classkey, relfile, lo, hi, names)
    modules: list = field(default_factory=list)    # (modname, relfile, names, opaque)
    files: list = field(default_factory=list)      # data files touched
    broken: list = field(default_factory=list)     # python files that no longer parse
    tests_files: list = field(default_factory=list)  # test modules changed
    notes: list = field(default_factory=list)


def modname(rel: str) -> str:
    return rel[:-3].replace("/", ".").removesuffix(".__init__")


def _span(node):
    lo = code_line(node) if hasattr(node, "lineno") else None
    return lo, getattr(node, "end_lineno", None)


def chain(tree, touches):
    """Nodes from the module down to the deepest def, lambda, class or
    statement that contains every touch."""
    path = [tree]
    node = tree
    while True:
        nxt = None
        for child in ast.iter_child_nodes(node):
            lo, hi = _span(child)
            if lo is None:
                # e.g. `arguments`: look inside for the touch
                if any(_contains_any(g, touches) for g in ast.walk(child) if hasattr(g, "lineno")):
                    nxt = child
                    break
                continue
            if all(lo <= t <= hi for t in touches):
                nxt = child
                break
        if nxt is None:
            return path
        path.append(nxt)
        node = nxt


def _contains_any(node, touches):
    lo, hi = _span(node)
    return lo is not None and any(lo <= t <= hi for t in touches)


SCOPES = (ast.Module, ast.ClassDef)
FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _units(chain_nodes):
    """(kind, name) of each def/lambda/class along a chain."""
    out = []
    for n in chain_nodes:
        if isinstance(n, FUNCS):
            out.append(("def", getattr(n, "name", "<lambda>")))
        elif isinstance(n, ast.ClassDef):
            out.append(("class", n.name))
    return out


def _in_body(func, touches) -> bool:
    body = func.body if isinstance(func.body, list) else [func.body]
    lo, hi = code_line(body[0]), body[-1].end_lineno
    return all(lo <= t <= hi for t in touches)


def _body_stmts(scope, touches):
    return [s for s in scope.body if _contains_any(s, touches)]


def _nested_funcs(stmt):
    """Defs and lambdas inside a statement, outermost only, in order."""
    out = []

    def walk(node):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, FUNCS):
                out.append(child)
            else:
                walk(child)

    walk(stmt)
    return out


_TREES: dict = {}


def _parse_cached(rel, src):
    """Trees of the pinned files; read-only, so patches can share them."""
    key = (rel, len(src), hash(src))
    if key not in _TREES:
        _TREES[key] = ast.parse(src)
    return _TREES[key]


def analyze_python(rel: str, old_src: str | None, new_src: str | None, change: Change, edits=()):
    mod = modname(rel)
    if old_src is None:
        change.modules.append((mod, rel, set(), True))
        change.notes.append(f"new module {rel}")
        return
    if new_src is None:
        change.broken.append(rel)
        return
    try:
        old_tree = _parse_cached(rel, old_src)
        new_tree = ast.parse(new_src)
    except SyntaxError:
        change.broken.append(rel)
        return
    old_lines, new_lines = old_src.split("\n"), new_src.split("\n")
    before = (len(change.funcs), len(change.lines), len(change.classes), len(change.modules))
    for old_touch, new_touch in edits:
        oc, nc = chain(old_tree, old_touch), chain(new_tree, new_touch)
        # the deepest def/lambda containing the edit on both sides, if they match
        o_idx = [i for i, n in enumerate(oc) if isinstance(n, FUNCS)]
        n_idx = [i for i, n in enumerate(nc) if isinstance(n, FUNCS)]
        if o_idx and n_idx and _units(oc[:o_idx[-1] + 1]) == _units(nc[:n_idx[-1] + 1]):
            k = len(o_idx) - 1
            of, nf = oc[o_idx[k]], nc[n_idx[k]]
            # A change to a nested def's signature, defaults or decorators
            # runs when the enclosing def runs, so it belongs to that def.
            while k > 0 and not (_in_body(of, old_touch) and _in_body(nf, new_touch)):
                k -= 1
                of, nf = oc[o_idx[k]], nc[n_idx[k]]
            if not same_node(of, nf, old_lines, new_lines):
                # Inside the body, only tests reaching the edited statement
                # can notice: execution is identical up to that point.
                stmts = [n for n in oc[oc.index(of) + 1:] if isinstance(n, ast.stmt)]
                if stmts and all(isinstance(t, int) for t in old_touch):
                    st = stmts[-1]
                    change.lines.append((rel, code_line(st), st.end_lineno, code_line(of), of.end_lineno))
                else:
                    change.funcs.append((rel, code_line(of), of.end_lineno))
            continue
        # otherwise compare the statements of the innermost class/module scope
        o_scope = [n for n in oc if isinstance(n, SCOPES)][-1]
        n_scope = [n for n in nc if isinstance(n, SCOPES)][-1]
        if _units([o_scope]) != _units([n_scope]):
            o_scope, n_scope = old_tree, new_tree
        olds = _body_stmts(o_scope, old_touch)
        news = _body_stmts(n_scope, new_touch)
        if [masked_dump(x) for x in olds] != [masked_dump(x) for x in news]:
            names = set()
            for stmt in olds + news:
                names |= bound_names(stmt)
            if isinstance(o_scope, ast.Module):
                # imports can fail or cycle at import time: treat as opaque
                imports = any(isinstance(n, (ast.Import, ast.ImportFrom)) for st in olds + news for n in ast.walk(st))
                change.modules.append((mod, rel, names, imports or not names))
            else:
                qual = ".".join(n for k, n in _units(oc[:oc.index(o_scope) + 1]) if k == "class")
                # plain attribute assignments can be narrowed to the code reading them
                attrs_only = all(
                    isinstance(st, (ast.Assign, ast.AnnAssign, ast.AugAssign))
                    and not any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                                for n in ast.walk(st))
                    for st in olds + news
                )
                change.classes.append(
                    (f"{mod}:{qual}", rel, code_line(o_scope), o_scope.end_lineno, names if attrs_only else None)
                )
            continue
        # same skeleton: the edit sits inside defs/lambdas of these statements
        for so, sn in zip(olds, news):
            for fo, fn in zip(_nested_funcs(so), _nested_funcs(sn)):
                if not same_node(fo, fn, old_lines, new_lines):
                    change.funcs.append((rel, code_line(fo), fo.end_lineno))
    if before == (len(change.funcs), len(change.lines), len(change.classes), len(change.modules)):
        change.notes.append(f"{rel}: same syntax tree")


class Index:
    def __init__(self, maps_dir: str, tests: list[dict]):
        with gzip.open(os.path.join(maps_dir, "testmap.json.gz"), "rt") as f:
            tm = json.load(f)
        with gzip.open(os.path.join(maps_dir, "static.json.gz"), "rt") as f:
            st = json.load(f)
        number = {t["id"]: t["number"] for t in tests}
        to_num = [number.get(tid) for tid in tm["tests"]]
        self.unmapped = sorted(set(number.values()) - {n for n in to_num if n is not None})
        self.tests = tests
        self.by_file: dict[str, list[int]] = {}
        for t in tests:
            self.by_file.setdefault(t["file"], []).append(t["number"])

        def conv(d):
            return {k: frozenset(to_num[i] for i in v if to_num[i] is not None) for k, v in d.items()}

        self.codes = conv(tm["codes"])
        self.types = conv(tm["types"])
        self.files = conv(tm["files"])
        # line coverage: file -> sorted [(line, tests)]
        line_sets = [frozenset(to_num[i] for i in t if to_num[i] is not None) for t in tm.get("line_sets", ())]
        self.lines: dict[str, tuple[list[int], list[frozenset]]] = {}
        for f, entries in tm.get("lines", {}).items():
            pairs = sorted((int(n), line_sets[k]) for n, k in entries.items())
            self.lines[f] = ([n for n, _ in pairs], [t for _, t in pairs])
        self.code_lines: dict[str, list[int]] = {}
        for key in self.codes:
            f, n = key.rsplit(":", 1)
            self.code_lines.setdefault(f, []).append(int(n))
        for v in self.code_lines.values():
            v.sort()
        self.subclasses = st["subclasses"]
        self.names = st["names"]
        self.class_names = st["class_names"]
        self.module_names = st["module_names"]
        self.importers = st["importers"]
        self.import_codes = set(st["import_codes"])
        self.import_class_codes = set(st["import_class_codes"])
        self.import_instances = set(st["import_instances"])
        self.class_refs = st["class_refs"]

    def tests_in_span(self, rel: str, lo: int, hi: int) -> set[int]:
        lines = self.code_lines.get(rel, [])
        out: set[int] = set()
        for n in lines[bisect_left(lines, lo):bisect_right(lines, hi)]:
            out |= self.codes[f"{rel}:{n}"]
        return out

    def tests_on_lines(self, rel: str, lo: int, hi: int) -> set[int] | None:
        """Tests that ran any line in lo..hi, or None without line coverage."""
        if not self.lines:
            return None
        nums, sets = self.lines.get(rel, ([], []))
        out: set[int] = set()
        for t in sets[bisect_left(nums, lo):bisect_right(nums, hi)]:
            out |= t
        return out

    def tests_using_class(self, key: str) -> set[int]:
        out: set[int] = set()
        for sub in self.subclasses.get(key, [key]):
            out |= self.types.get(sub, frozenset())
        return out

    def tests_naming(self, names) -> set[int]:
        out: set[int] = set()
        for name in names:
            for key in self.names.get(name, ()):
                out |= self.codes.get(key, frozenset())
        return out

    def tests_using_module(self, mod: str, rel: str) -> set[int]:
        out = self.tests_in_span(rel, 0, 10**9)
        prefix = mod + ":"
        for key, tests in self.types.items():
            if key.startswith(prefix):
                out |= tests
        return out

    def tests_opening(self, path: str) -> set[int]:
        out = set(self.files.get(path, ()))
        parent = os.path.dirname(path)
        while parent:
            # a test that lists a directory sees files added to or removed from it
            out |= self.files.get(parent, frozenset())
            parent = os.path.dirname(parent)
        return out


def candidates(diffs: list[FileDiff], src_root: str, index: Index, sources: dict) -> tuple[set[int], Change]:
    """Tests that can notice the patch, and the analysis behind them.

    sources is filled with {relpath: new text or None} for every changed file.
    """
    change = Change()
    for d in diffs:
        rel_old, rel_new = d.old, d.new
        old_text = None
        if rel_old is not None:
            try:
                with open(os.path.join(src_root, rel_old), encoding="utf-8") as f:
                    old_text = f.read()
            except FileNotFoundError:
                old_text = None
        new_text, edits = apply_hunks(old_text or "", d.hunks) if rel_new is not None else (None, [])
        if rel_old and rel_new and rel_old != rel_new:
            sources[rel_old] = None
        if rel_new:
            sources[rel_new] = new_text
        elif rel_old:
            sources[rel_old] = None
        for rel, old_t, new_t in {(rel_old or rel_new, old_text, new_text if rel_old == rel_new or not rel_old else None)} | (
            {(rel_new, None, new_text)} if rel_old and rel_new and rel_old != rel_new else set()
        ):
            if rel.startswith("sqlglot/") and rel.endswith(".py"):
                analyze_python(rel, old_t, new_t, change, edits)
                # some tests read sqlglot's source as text
                change.files.append((rel, old_t is None or new_t is None, old_t, new_t))
            elif rel.startswith("tests/") and rel.endswith(".py"):
                change.tests_files.append((rel, old_t, new_t))
            else:
                change.files.append((rel, old_t is None or new_t is None, old_t, new_t))

    out: set[int] = set()
    for rel, lo, hi in change.funcs:
        out |= index.tests_in_span(rel, lo, hi)
    for rel, lo, hi, flo, fhi in change.lines:
        on_lines = index.tests_on_lines(rel, lo, hi)
        out |= index.tests_in_span(rel, flo, fhi) if on_lines is None else on_lines
    for key, rel, lo, hi, names in change.classes:
        out |= class_tests(index, key, rel, lo, hi, names)
    for mod, rel, names, opaque in change.modules:
        out |= module_tests(index, src_root, mod, rel, names, opaque, 0)
    for rel in change.broken:
        out |= index.tests_using_module(modname(rel), rel)
        out |= module_tests(index, src_root, modname(rel), rel, set(), True, 0)
    for rel, added_or_removed, old_t, new_t in change.files:
        if added_or_removed:
            out |= index.tests_opening(rel)
        else:
            out |= index.files.get(rel, frozenset())
    for rel, old_t, new_t in change.tests_files:
        out |= test_file_tests(index, rel, old_t, new_t)
    return out, change


def class_users(index: Index, key: str) -> set[int]:
    """Tests that use a class: through it, a subclass, an enclosing class, or
    a class whose body refers to one of those (a dialect naming its generator)."""
    keys = set(index.subclasses.get(key, [key]))
    mod, qual = key.split(":", 1)
    parts = qual.split(".")
    keys |= {f"{mod}:{'.'.join(parts[:i])}" for i in range(1, len(parts))}
    for k in list(keys):
        for ref in index.class_refs.get(k, ()):
            keys |= set(index.subclasses.get(ref, [ref]))
    out: set[int] = set()
    for k in keys:
        out |= index.types.get(k, frozenset())
    return out


def class_tests(index: Index, key, rel, lo, hi, names) -> set[int]:
    users = class_users(index, key) | index.tests_in_span(rel, lo, hi)
    if not names:
        # methods added or removed, or something other than plain attributes
        return users or index.tests_using_module(modname(rel), rel)
    readers = set()
    for name in names:
        readers |= set(index.names.get(name, ()))
    if not users:
        return index.tests_naming(names)
    # Read while classes are built (with the class in hand), or through an
    # instance made while loading: anything derived from it may differ.
    built = readers & index.import_class_codes
    if index.import_instances & set(index.subclasses.get(key, [key])):
        built |= readers & index.import_codes
    if not readers or built:
        return users
    readable = set()
    for r in readers:
        readable |= index.codes.get(r, frozenset())
    return users & readable


def module_rel(src_root, mod) -> str:
    rel = mod.replace(".", "/") + ".py"
    if not os.path.exists(os.path.join(src_root, rel)):
        rel = mod.replace(".", "/") + "/__init__.py"
    return rel


def module_tests(index: Index, src_root, mod, rel, names, opaque, depth) -> set[int]:
    if opaque:
        # anything could have changed: whoever uses this module or imports it
        out = index.tests_using_module(mod, rel)
        for other in index.importers.get(mod, ()):
            if other != mod:
                out |= index.tests_using_module(other, module_rel(src_root, other))
        return out
    out = index.tests_naming(names)
    for name in names:
        for cls in index.class_names.get(name, ()):
            out |= index.tests_using_class(cls)
        if depth < 2:
            for other in index.module_names.get(name, ()):
                if other == mod:
                    continue
                orel = module_rel(src_root, other)
                derived = derived_names(os.path.join(src_root, orel), name)
                out |= module_tests(index, src_root, other, orel, derived, not derived, depth + 1)
    return out


def derived_names(path: str, name: str) -> set[str]:
    """Names bound at the top of a module by statements that mention `name`."""
    try:
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
    except (OSError, SyntaxError):
        return set()
    out = set()
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        mentioned = {n.id for n in ast.walk(stmt) if isinstance(n, ast.Name)}
        mentioned |= {n.attr for n in ast.walk(stmt) if isinstance(n, ast.Attribute)}
        mentioned |= {(a.asname or a.name) for a in ast.walk(stmt) if isinstance(a, ast.alias)}
        if name in mentioned:
            out |= bound_names(stmt)
    return out


def test_file_tests(index: Index, rel, old_t, new_t) -> set[int]:
    """A changed test module: its changed test methods, or all of it."""
    whole = set(index.by_file.get(rel, ()))
    # support modules (helpers, conftest) reach every test file that imports them
    stem = os.path.splitext(os.path.basename(rel))[0]
    if not os.path.basename(rel).startswith("test_"):
        for f, nums in index.by_file.items():
            whole |= set(nums)
        return whole
    if old_t is None or new_t is None:
        return whole
    try:
        old_tree, new_tree = ast.parse(old_t), ast.parse(new_t)
    except SyntaxError:
        return whole
    if ast.dump(old_tree) == ast.dump(new_tree):
        return set()
    of, os_ = units(old_tree)
    nf, ns = units(new_tree)
    if any(os_[p][1] != ns.get(p, (None, None))[1] for p in os_) or set(os_) != set(ns):
        return whole
    changed = set()
    for path in set(of) | set(nf):
        if path not in of or path not in nf or ast.dump(of[path]) != ast.dump(nf[path]):
            top = path[:2]
            if len(top) == 2 and top[0][0].startswith("class ") and top[1][0].startswith("test"):
                changed.add(f"{rel}::{top[0][0].removeprefix('class ')}::{top[1][0]}")
            else:
                return whole
    ids = {t["id"]: t["number"] for t in index.tests if t["file"] == rel}
    return {ids[c] for c in changed if c in ids} or set()
