#!/usr/bin/env python3
"""Build modules/index.json: the catalogue the panel browses and installs modules from.

One entry per module directory under modules/: what its module.json says, plus every file
with its SHA-256. Run it after changing a module (and raise the module's version when its
behaviour changes); `--check` fails when the index is not current, for CI.

    tools/module-index.py            # rewrite modules/index.json
    tools/module-index.py --check
"""
import hashlib
import json
import os
import re
import sys

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "modules")
ID = re.compile(r"[a-z][a-z0-9_]{1,30}")


def build() -> dict:
    mods = []
    for mid in sorted(os.listdir(ROOT)):
        d = os.path.join(ROOT, mid)
        if not os.path.isfile(os.path.join(d, "__init__.py")):
            continue
        with open(os.path.join(d, "module.json")) as f:
            man = json.load(f)
        if man.get("id") != mid or not ID.fullmatch(mid):
            sys.exit(f"{mid}: module.json must have \"id\": \"{mid}\" (lower-case letters, digits, _)")
        for k in ("version", "kind", "name", "description"):
            if not man.get(k):
                sys.exit(f"{mid}: module.json lacks \"{k}\"")
        files = []
        for base, dirs, names in os.walk(d):
            dirs[:] = sorted(x for x in dirs if x != "__pycache__")
            for n in sorted(names):
                p = os.path.join(base, n)
                if os.path.islink(p):
                    sys.exit(f"{mid}: {n} is a link; modules hold regular files only")
                with open(p, "rb") as f:
                    files.append({"path": os.path.relpath(p, d), "sha256": hashlib.sha256(f.read()).hexdigest(),
                                  "exec": os.access(p, os.X_OK)})
        mods.append({**{k: man[k] for k in ("id", "version", "kind", "name", "description")}, "files": files})
    return {"format": 1, "modules": mods}


text = json.dumps(build(), ensure_ascii=False, indent=1) + "\n"
path = os.path.join(ROOT, "index.json")
if "--check" in sys.argv:
    cur = open(path).read() if os.path.exists(path) else ""
    sys.exit(0 if cur == text else "modules/index.json is not current: run tools/module-index.py")
open(path, "w").write(text)
print(f"wrote {os.path.relpath(path)}")
