"""Soloist on the device: its unit, key, accounts and preferences, and its PipeWire stream.
Used by the Spotify Lossless module only."""
from __future__ import annotations

import datetime as dt
import glob
import logging
import os
import re

import collect
from collect import read, run, audio_uid

SOLOIST_HOME = collect.AUDIO_HOME
SOLOIST_UNIT = f"{SOLOIST_HOME}/.config/systemd/user/soloist.service"
SOLOIST_ENV = "/etc/soloist.env"
BUILD_VALID_DAYS = 90  # Spotify's builds stop working after this long


def soloist_unit_text() -> str:
    return read(SOLOIST_UNIT)


def soloist_device_name() -> str:
    m = re.search(r'--device-name\s+"([^"]+)"', soloist_unit_text())
    return m.group(1) if m else ""


def prefs_path() -> str | None:
    p = sorted(glob.glob(f"{SOLOIST_HOME}/.local/share/soloist/settings/Users/*/prefs"))
    return p[0] if p else None


def account_dirs() -> list[str]:
    """Real account directories only (no symlinks: the tree is the soloist user's)."""
    base = f"{SOLOIST_HOME}/.local/share/soloist/settings/Users"
    try:
        with os.scandir(base) as it:
            return sorted(f"{base}/{e.name}/" for e in it if e.is_dir(follow_symlinks=False))
    except OSError:
        return []


def prefs_paths() -> list[str]:
    """One prefs file per Spotify account that has used the device (you, and guests)."""
    return sorted(glob.glob(f"{SOLOIST_HOME}/.local/share/soloist/settings/Users/*/prefs"))


def _norm_on(path: str) -> bool:
    m = re.search(r"^audio\.normalize_v2=(\w+)", read(path), re.M)
    return True if not m else m.group(1) != "false"  # Spotify's default is on


# What every account on the device needs (a guest's new account has neither): Spotify's
# defaults are normalization on and a lossy bitrate. Quality 5 is Lossless; an account without
# Lossless in its plan gets the best it has.
LOSSLESS = ("audio.play_bitrate_non_metered_enumeration", "5")


def required_prefs(normalization: bool) -> dict[str, str]:
    return {"audio.normalize_v2": "true" if normalization else "false", LOSSLESS[0]: LOSSLESS[1]}


def _pref(text: str, key: str) -> str | None:
    m = re.search(rf"^{re.escape(key)}=(.*)$", text, re.M)
    return m.group(1).strip() if m else None


def normalization_enabled() -> bool | None:
    """On for any account (a guest's new account starts with Spotify's default, on)."""
    ps = prefs_paths()
    return any(_norm_on(p) for p in ps) if ps else None


def normalization_mismatch(want: bool) -> list[str]:
    """Accounts whose prefs differ from what every account needs (normalization as wanted,
    and Lossless). A missing normalize line is Spotify's default, on."""
    bad = []
    for d in account_dirs():
        text = read(d + "prefs")
        norm_on = _pref(text, "audio.normalize_v2") != "false"
        if norm_on != want or _pref(text, LOSSLESS[0]) != LOSSLESS[1]:
            bad.append(d + "prefs")
    return bad


_SET_NORM = r"""
import os, re, sys
path, lines = sys.argv[1], sys.argv[2:]
try:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as f:
        text = f.read()
except FileNotFoundError:
    text = ""  # a new account Soloist has not written prefs for yet
for line in lines:
    key = line.split("=", 1)[0]
    pat = r"^" + re.escape(key) + r"=.*$"
    text = re.sub(pat, line, text, flags=re.M) if re.search(pat, text, re.M) \
        else (text.rstrip("\n") + "\n" if text.strip() else "") + line + "\n"
tmp = path + ".mscos-tmp"
fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
with os.fdopen(fd, "w") as f:
    f.write(text)
os.replace(tmp, path)
"""


async def set_normalization_file(path: str, enabled: bool) -> bool:
    """Edit one account's prefs AS THE SOLOIST USER: the Users/<account>/ tree is the
    soloist user's (guests add accounts), so a root write there could be sent elsewhere
    with a symlink. As that user it can only touch that user's own files."""
    if not path.startswith(f"{SOLOIST_HOME}/.local/share/soloist/settings/Users/") or "/../" in path:
        return False
    lines = [f"{k}={v}" for k, v in required_prefs(enabled).items()]
    rc, _, err = await run(["python3", "-I", "-c", _SET_NORM, path, *lines], as_user=True, timeout=15)
    if rc != 0:
        logging.getLogger("collect").warning("prefs %s: %s", path, err.strip()[-200:])
    return rc == 0


_version_cache: tuple[float, str] = (0.0, "")


async def soloist_version() -> str:
    """`soloist --version`, re-run only when the binary changes (it is a 29 MB executable)."""
    global _version_cache
    try:
        mtime = os.stat("/usr/local/bin/soloist").st_mtime
    except OSError:
        return ""
    if mtime != _version_cache[0]:
        rc, out, _ = await run(["/usr/local/bin/soloist", "--version"], timeout=10)
        _version_cache = (mtime, out.strip())
    return _version_cache[1]


async def soloist_info() -> dict:
    version = await soloist_version()
    build = re.search(r"\((\d{8})\)", version)
    days_left = None
    if build:
        bdate = dt.datetime.strptime(build.group(1), "%Y%m%d").date()
        days_left = (bdate + dt.timedelta(days=BUILD_VALID_DAYS) - dt.date.today()).days
    rc2, active, _ = await run(["systemctl", "--user", "is-active", "soloist.service"], as_user=True)
    rc3, rt_active, _ = await run(["systemctl", "--user", "is-active", "soloist-rt.service"], as_user=True)
    rc4, timer, _ = await run("systemctl show soloist-update.timer -p NextElapseUSecRealtime -p LastTriggerUSec --value")
    tl = timer.strip().splitlines()
    return {
        "version": version,
        "build_date": build.group(1) if build else None,
        "days_left": days_left,
        "service": active.strip(),
        "rt_watcher": rt_active.strip(),
        "device_name": soloist_device_name(),
        "normalization": normalization_enabled(),
        "update_next": tl[0] if tl else "",
        "update_last": tl[1] if len(tl) > 1 else "",
        "api_key": api_key_info(),
    }


def api_key_info() -> dict:
    """Whether a Soloist key is configured (never the key itself)."""
    text = read(SOLOIST_ENV)
    try:
        changed = int(os.stat(SOLOIST_ENV).st_mtime)
    except OSError:
        changed = None
    return {"set": bool(re.search(r"^SOLOIST_API_KEY=\S{8,}", text, re.M)), "changed": changed}


# --------------------------------------------------------------------------- Spotify stream volume
# Soloist applies the Spotify volume as PipeWire stream volume on its "spotify" stream node
# (channelVolumes = (v/100)^3), and reads channelVolumes back: resetting them makes it report
# 100 % to Spotify. Setting softVolumes (what audioconvert actually multiplies by) to 1.0
# leaves channelVolumes, so Soloist sees nothing, and the samples pass untouched. Each new
# channelVolumes from Soloist re-applies them, so softVolumes is set again after every change.
# (Verified 2026-11-10: Spotify 50 %, channelVolumes 0.125, softVolumes 1.0 -> bit-perfect 0.0 dB,
# no echo.) volbridge.py drives this; the slider drives the amplifier instead.
_spotify_nodes: dict[int, list | None] = {}   # node id -> last channelVolumes seen


def spotify_stream_changed(o: dict) -> int | None:
    """A pw-dump object: Soloist's output stream with new channelVolumes? Returns its id."""
    if o.get("type") != "PipeWire:Interface:Node":
        return None
    oid, info = o.get("id"), o.get("info")
    if info is None:  # removed
        _spotify_nodes.pop(oid, None)
        return None
    props = info.get("props") or {}
    if props:
        if props.get("media.class") == "Stream/Output/Audio" and (
                props.get("application.process.binary") == "soloist"
                or str(props.get("node.name", "")).lower() == "spotify"
                or props.get("application.name") == "Spotify"):
            _spotify_nodes.setdefault(oid, None)
        else:
            _spotify_nodes.pop(oid, None)
    if oid not in _spotify_nodes:
        return None
    for p in (info.get("params") or {}).get("Props") or []:
        vols = p.get("channelVolumes")
        if vols is not None and vols != _spotify_nodes[oid]:
            _spotify_nodes[oid] = vols
            return oid
    return None


async def spotify_stream_unity(oid: int | None = None):
    """softVolumes to 1.0 on the Spotify stream(s): what reaches the DAC is untouched."""
    for n in ([oid] if oid is not None else list(_spotify_nodes)):
        ch = len(_spotify_nodes.get(n) or [0, 0]) or 2
        vols = ", ".join(["1.0"] * ch)
        await run(["pw-cli", "set-param", str(n), "Props", f"{{ softVolumes: [ {vols} ] }}"], as_user=True, timeout=5)
