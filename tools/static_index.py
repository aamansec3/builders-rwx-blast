"""Index sqlglot's source at the pinned commit for the selector.

usage: static_index.py SQLGLOT_CHECKOUT OUT.json.gz IMPORT_CODES.json

  subclasses: "module:Qual.Name" -> every class deriving from it, itself included
  names:      identifier -> code keys ("file:firstlineno") of functions that mention it
  class_names: identifier -> classes whose body (outside methods) mentions it
  module_names: identifier -> modules whose top level mentions it
  importers:  module -> modules that import it (anywhere in their source)
  import_codes: code keys that run while sqlglot and its dialects load
  class_refs: class -> classes holding it as an attribute (a dialect's generator)
"""

import ast
import collections
import gzip
import importlib
import json
import os
import pkgutil
import sys


def code_line(node):
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.decorator_list:
        return min([node.lineno] + [d.lineno for d in node.decorator_list])
    return node.lineno


def mentioned(node):
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and n.value.isidentifier():
            out.add(n.value)  # getattr(x, "NAME"), klass.__dict__.get("NAME")
        elif isinstance(n, ast.alias):
            out.add((n.asname or n.name).split(".")[0])
    return out


def shallow_mentioned(stmts):
    """Names in statements, not descending into defs, classes or lambdas."""
    out = set()
    stack = list(stmts)
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            stack.extend(n.decorator_list)
            stack.extend(n.args.defaults)
            stack.extend(d for d in n.args.kw_defaults if d)
            continue
        if isinstance(n, ast.ClassDef):
            stack.extend(n.bases)
            stack.extend(n.decorator_list)
            continue
        if isinstance(n, ast.Lambda):
            continue
        if isinstance(n, ast.Name):
            out.add(n.id)
        elif isinstance(n, ast.Attribute):
            out.add(n.attr)
        elif isinstance(n, ast.alias):
            out.add((n.asname or n.name).split(".")[0])
        stack.extend(ast.iter_child_nodes(n))
    return out


def main(root, out, import_codes):
    sys.path.insert(0, root)
    names = collections.defaultdict(set)
    class_names = collections.defaultdict(set)
    module_names = collections.defaultdict(set)
    importers = collections.defaultdict(set)
    class_refs = collections.defaultdict(set)
    for dirpath, _, files in os.walk(os.path.join(root, "sqlglot")):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(dirpath, fn)
            rel = os.path.relpath(path, root)
            mod = rel[:-3].replace(os.sep, ".").removesuffix(".__init__")
            tree = ast.parse(open(path).read())
            for name in shallow_mentioned(tree.body):
                module_names[name].add(mod)
            package = mod if fn == "__init__.py" else mod.rsplit(".", 1)[0]
            for node in ast.walk(tree):
                targets = []
                if isinstance(node, ast.Import):
                    targets = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    base = node.module or ""
                    if node.level:
                        parts = package.split(".")
                        parts = parts[: len(parts) - node.level + 1]
                        base = ".".join(parts + ([base] if base else []))
                    targets = [base] + [f"{base}.{a.name}" for a in node.names]
                for target in targets:
                    if target.startswith("sqlglot"):
                        importers[target].add(mod)

            def visit(node, qual):
                for child in ast.iter_child_nodes(node):
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                        key = f"{rel}:{code_line(child)}"
                        for name in mentioned(child):
                            names[name].add(key)
                        visit(child, qual)
                    elif isinstance(child, ast.ClassDef):
                        q = qual + [child.name]
                        for name in shallow_mentioned(child.body):
                            class_names[name].add(f"{mod}:{'.'.join(q)}")
                        visit(child, q)
                    else:
                        visit(child, qual)

            visit(tree, [])

    import sqlglot

    for info in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
        if not info.name.endswith("__main__"):
            importlib.import_module(info.name)
    from sqlglot.dialects.dialect import Dialect, Dialects

    for d in Dialects:
        if d.value:
            Dialect.get_or_raise(d.value)
    classes = {}

    def collect(cls):
        key = f"{cls.__module__}:{cls.__qualname__}"
        if key in classes or not cls.__module__.startswith("sqlglot"):
            return
        classes[key] = cls
        for v in list(vars(cls).values()) + type.__subclasses__(cls):
            if isinstance(v, type):
                collect(v)

    for m in list(sys.modules.values()):
        if m and m.__name__.startswith("sqlglot"):
            for v in list(vars(m).values()):
                if isinstance(v, type):
                    collect(v)
    # `Generator = ClickHouseGenerator`, `tokenizer_class`: a class that hands out another
    for key, cls in classes.items():
        for value in vars(cls).values():
            if isinstance(value, type) and value is not cls and value.__module__.startswith("sqlglot"):
                class_refs[f"{value.__module__}:{value.__qualname__}"].add(key)
    subclasses = {}
    for key, cls in classes.items():
        seen, stack = set(), [cls]
        while stack:
            c = stack.pop()
            if c in seen:
                continue
            seen.add(c)
            stack.extend(type.__subclasses__(c))
        subclasses[key] = sorted(f"{c.__module__}:{c.__qualname__}" for c in seen)

    data = {
        "subclasses": subclasses,
        "names": {k: sorted(v) for k, v in names.items()},
        "class_names": {k: sorted(v) for k, v in class_names.items()},
        "module_names": {k: sorted(v) for k, v in module_names.items()},
        "importers": {k: sorted(v) for k, v in importers.items()},
        **{f"import_{k}": v for k, v in json.load(open(import_codes)).items()},
        "class_refs": {k: sorted(v) for k, v in class_refs.items()},
    }
    with gzip.open(out, "wt") as f:
        json.dump(data, f, separators=(",", ":"))
    print({k: len(v) for k, v in data.items()}, file=sys.stderr)


if __name__ == "__main__":
    main(*sys.argv[1:])
