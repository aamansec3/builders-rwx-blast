"""List the sqlglot code that runs while sqlglot and every dialect load.

usage: import_codes.py SQLGLOT_CHECKOUT OUT.json

Class attributes read by this code (metaclasses, __init_subclass__, class
decorators) feed structures built at import time, so the selector cannot
narrow a change to such an attribute down to the code that reads it later.
Writes {"codes", "class_codes" (ran with a class as first argument),
"instances" (classes instantiated while loading)}.
"""

import importlib
import json
import os
import pkgutil
import sys

root = os.path.realpath(sys.argv[1]) + os.sep
sys.path.insert(0, root)
mon = sys.monitoring
codes = set()
class_codes = set()  # ran with a class as first argument: building classes
instances = set()  # classes instantiated while loading


def start(code, offset):
    if not code.co_filename.startswith(root + "sqlglot"):
        return mon.DISABLE
    key = f"{code.co_filename[len(root):]}:{code.co_firstlineno}"
    codes.add(key)
    if code.co_argcount:
        first = sys._getframe(1).f_locals.get(code.co_varnames[0])
        if isinstance(first, type):
            class_codes.add(key)
        elif code.co_name == "__init__" and first is not None:
            t = type(first)
            instances.add(f"{t.__module__}:{t.__qualname__}")


mon.use_tool_id(4, "imports")
mon.register_callback(4, mon.events.PY_START, start)
mon.set_events(4, mon.events.PY_START)
import sqlglot  # noqa: E402

for info in pkgutil.walk_packages(sqlglot.__path__, "sqlglot."):
    if not info.name.endswith("__main__"):
        importlib.import_module(info.name)
from sqlglot.dialects.dialect import Dialect, Dialects  # noqa: E402

for d in Dialects:
    if d.value:
        Dialect.get_or_raise(d.value)
mon.set_events(4, 0)
json.dump({"codes": sorted(codes), "class_codes": sorted(class_codes), "instances": sorted(instances)},
          open(sys.argv[2], "w"))
print(len(codes), len(class_codes), len(instances), file=sys.stderr)
