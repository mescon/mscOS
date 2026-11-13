"""The listener's own folders for their Spotify playlists.

Spotify's app lets one sort playlists into folders, but its Web API tells nothing of them: a
playlist comes without a word of where it is kept. So the panel has folders of its own, kept on
the device: a tree of names, and for each playlist the folder it was put in. Nothing here is
sent to Spotify, and nothing here touches the playlists themselves.
"""
from __future__ import annotations

import json
import os
import re
import secrets

MAX_FOLDERS, MAX_PLACED, MAX_DEPTH, MAX_NAME = 300, 5000, 6, 60
FOLDER_ID = re.compile(r"f[0-9a-f]{10}")
PLAYLIST_ID = re.compile(r"[A-Za-z0-9]{22}")


class Refused(Exception):
    """A change that cannot be made; str() says why, as a key for the page's words."""


def empty() -> dict:
    return {"folders": [], "place": {}}


def clean(d) -> dict:
    """What was read from the file, with anything that does not hold together left out."""
    out = empty()
    if not isinstance(d, dict):
        return out
    seen = set()
    for f in d.get("folders") or []:
        if isinstance(f, dict) and FOLDER_ID.fullmatch(str(f.get("id"))) and f["id"] not in seen and str(f.get("name") or "").strip():
            seen.add(f["id"])
            out["folders"].append({"id": f["id"], "name": str(f["name"]).strip()[:MAX_NAME], "parent": f.get("parent")})
    for f in out["folders"]:
        if f["parent"] not in seen or f["parent"] == f["id"]:
            f["parent"] = None
    for f in out["folders"]:                       # (a ring of folders each inside the next: opened up)
        if _depth(out, f["id"]) is None:
            f["parent"] = None
    out["place"] = {p: f for p, f in (d.get("place") or {}).items() if PLAYLIST_ID.fullmatch(str(p)) and f in seen}
    return out


def _depth(d: dict, fid: str | None) -> int | None:
    """How deep the folder lies (1: at the top); None when its parents run in a ring."""
    by, n = {f["id"]: f for f in d["folders"]}, 0
    while fid is not None:
        n += 1
        if n > len(by) + 1 or fid not in by:
            return None
        fid = by[fid]["parent"]
    return n


def _name(name) -> str:
    name = " ".join(str(name or "").split())[:MAX_NAME]
    if not name or any(ord(c) < 32 for c in name):
        raise Refused("name")
    return name


def change(d: dict, op: dict) -> dict:
    """One change, made to a copy: add, rename, remove or move a folder, or put a playlist in
    one (or back out, with no folder). Raises Refused when it cannot be."""
    d = clean(json.loads(json.dumps(d)))
    by = {f["id"]: f for f in d["folders"]}
    kind = op.get("op")

    def folder(key, none_ok=False):
        fid = op.get(key)
        if fid is None and none_ok:
            return None
        if fid not in by:
            raise Refused("unknown")
        return fid

    if kind == "add":
        parent = folder("parent", none_ok=True)
        if len(d["folders"]) >= MAX_FOLDERS or (_depth(d, parent) or 0) >= MAX_DEPTH:
            raise Refused("full")
        d["folders"].append({"id": "f" + secrets.token_hex(5), "name": _name(op.get("name")), "parent": parent})
    elif kind == "rename":
        by[folder("id")]["name"] = _name(op.get("name"))
    elif kind == "remove":                         # what was in it moves up to where the folder stood
        fid = folder("id")
        up = by[fid]["parent"]
        for f in d["folders"]:
            if f["parent"] == fid:
                f["parent"] = up
        for p, at in list(d["place"].items()):
            if at == fid:
                if up is None:
                    del d["place"][p]
                else:
                    d["place"][p] = up
        d["folders"] = [f for f in d["folders"] if f["id"] != fid]
    elif kind == "move_folder":
        fid, parent = folder("id"), folder("parent", none_ok=True)
        was = by[fid]["parent"]
        by[fid]["parent"] = parent
        if _depth(d, fid) is None:                 # into itself, or into one of its own
            by[fid]["parent"] = was
            raise Refused("into itself")
        if max(_depth(d, f["id"]) or 0 for f in d["folders"]) > MAX_DEPTH:
            by[fid]["parent"] = was
            raise Refused("full")
    elif kind == "place":
        pid = str(op.get("playlist") or "")
        if not PLAYLIST_ID.fullmatch(pid):
            raise Refused("unknown")
        to = folder("folder", none_ok=True)
        if to is None:
            d["place"].pop(pid, None)
        else:
            if pid not in d["place"] and len(d["place"]) >= MAX_PLACED:
                raise Refused("full")
            d["place"][pid] = to
    else:
        raise Refused("unknown")
    return d


def load(path: str) -> dict:
    try:
        with open(path) as f:
            return clean(json.load(f))
    except (OSError, ValueError):
        return empty()


def save(path: str, d: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(d, f)
    os.replace(path + ".tmp", path)
