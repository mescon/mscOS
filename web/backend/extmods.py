"""Modules that do not come with mscOS: a directory each under <state>/plugins.

A module there is a Python package like the shipped ones (see docs/MODULES.md), with a
module.json beside its __init__.py:

    {"id": "mymodule", "version": "1.2.0", "source": "https://example.org/mymodule/module.json"}

"source" is optional: the address of the same file at the publisher, where it also says
where the archive is: {"id", "version", "archive": "https://…/mymodule-1.2.0.tar.gz",
"sha256": "…"}. The panel installs a module from such an address, and looks there for a
newer version when the user checks for updates.

A module runs inside the panel, as root. Only someone logged in can install one, the panel
says so before it does, and a module directory that anyone but root can write is not loaded.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import stat
import tarfile

import collect

DIR = os.environ.get("MSCOS_PLUGINS_DIR") or os.path.join(collect.STATE_DIR, "plugins")
MAX_BYTES = 8 * 1024 * 1024
ID = re.compile(r"[a-z][a-z0-9_]{1,30}")
VERSION = re.compile(r"[0-9A-Za-z.+~-]{1,40}")


def safe_dir(path: str) -> bool:
    """Owned by root and writable by root only, all the way down: code in it runs as root."""
    for root, dirs, files in os.walk(path):
        for p in [root] + [os.path.join(root, f) for f in files]:
            st = os.lstat(p)
            if st.st_uid != 0 or st.st_mode & (stat.S_IWGRP | stat.S_IWOTH) or stat.S_ISLNK(st.st_mode):
                return False
    return True


def manifest_of(path: str) -> dict:
    try:
        with open(os.path.join(path, "module.json")) as f:
            m = json.load(f)
        return m if isinstance(m, dict) else {}
    except (OSError, ValueError):
        return {}


def check_manifest(m: dict, remote: bool = False) -> dict:
    """The fields the panel uses, validated; raises ValueError with what is wrong."""
    if not isinstance(m, dict) or not ID.fullmatch(str(m.get("id", ""))):
        raise ValueError("id")
    if not VERSION.fullmatch(str(m.get("version", ""))):
        raise ValueError("version")
    out = {"id": m["id"], "version": str(m["version"])}
    for k in ("source", "archive"):
        v = m.get(k)
        if v is not None:
            if not (isinstance(v, str) and v.startswith("https://") and len(v) < 500):
                raise ValueError(k)
            out[k] = v
    if remote:
        if "archive" not in out or not re.fullmatch(r"[0-9a-f]{64}", str(m.get("sha256", ""))):
            raise ValueError("archive/sha256")
        out["sha256"] = m["sha256"]
    return out


def unpack(data: bytes, sha256: str, mid: str, source: str, version: str, dest_root: str = DIR) -> str:
    """Verify and unpack a module archive (tar, optionally gzip) into <dest_root>/<id>.
    The archive holds the package's files, at its top level or in one directory. Only
    regular files and directories are taken, none outside the module's own directory."""
    if len(data) > MAX_BYTES:
        raise ValueError("too large")
    if hashlib.sha256(data).hexdigest() != sha256:
        raise ValueError("sha256")
    new = os.path.join(dest_root, f".{mid}.new")
    shutil.rmtree(new, ignore_errors=True)
    os.makedirs(new, mode=0o755)
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
            members = [m for m in tar.getmembers() if m.isfile() or m.isdir()]
            if len(members) != len(tar.getmembers()):
                raise ValueError("links or devices in the archive")
            tops = {m.name.split("/", 1)[0] for m in members}
            strip = (tops.pop() + "/") if len(tops) == 1 and not any(m.name == "__init__.py" for m in members) else ""
            total = 0
            for m in members:
                name = m.name[len(strip):] if strip and (m.name + "/").startswith(strip) else m.name
                name = os.path.normpath(name)
                if name in (".", "") or name.startswith(("..", "/")) or "\x00" in name:
                    if m.isdir():
                        continue
                    raise ValueError("path")
                target = os.path.join(new, name)
                if m.isdir():
                    os.makedirs(target, mode=0o755, exist_ok=True)
                    continue
                total += m.size
                if total > 4 * MAX_BYTES:
                    raise ValueError("too large")
                os.makedirs(os.path.dirname(target), mode=0o755, exist_ok=True)
                with tar.extractfile(m) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                os.chmod(target, 0o644)
        if not os.path.isfile(os.path.join(new, "__init__.py")):
            raise ValueError("no __init__.py")
        with open(os.path.join(new, "module.json"), "w") as f:  # what was installed, and from where
            json.dump({"id": mid, "version": version, "source": source}, f)
        final = os.path.join(dest_root, mid)
        old = os.path.join(dest_root, f".{mid}.old")
        shutil.rmtree(old, ignore_errors=True)
        if os.path.exists(final):
            os.rename(final, old)
        os.rename(new, final)
        shutil.rmtree(old, ignore_errors=True)
        return final
    except Exception:
        shutil.rmtree(new, ignore_errors=True)
        raise


def remove(mid: str, dest_root: str = DIR) -> bool:
    if not ID.fullmatch(mid):
        return False
    p = os.path.join(dest_root, mid)
    if not os.path.isdir(p):
        return False
    shutil.rmtree(p)
    return True
