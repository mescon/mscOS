"""The board profile: what differs between the boards mscOS runs on (which core is for
audio, the USB controllers' interrupt names, a blocking thermal sensor, ...). Profiles are
KEY="value" files in /usr/share/mscos/boards; /etc/mscos/board.conf links to the one in use
(chosen at setup by mscos-board), /etc/mscos/board.local.conf overrides single keys."""
from __future__ import annotations

import os
import re

DEFAULTS = {"BOARD_NAME": "", "BOARD_MATCH": "", "AUDIO_CPU": "", "USB_IRQ_MATCH": "[exo]hci[-_]hcd",
            "CPU_FIXED_KHZ": "", "THERMAL_OFF": "", "NET_SPEED": "", "LEDS": "", "DT_OVERLAY": "",
            "DT_OFF_NODE": "", "WATCHDOG": "", "USB_PORTS": "", "POWER_BUTTON": "", "STORAGE": ""}
FILES = ("/etc/mscos/board.conf", "/etc/mscos/board.local.conf")
_cache: tuple[tuple, dict] | None = None


def parse(text: str) -> dict:
    out = {}
    for m in re.finditer(r'^([A-Z_]+)=(?:"([^"]*)"|\'([^\']*)\'|(\S*))\s*(?:#.*)?$', text, re.M):
        out[m.group(1)] = m.group(2) if m.group(2) is not None else m.group(3) if m.group(3) is not None else m.group(4)
    return out


def profile() -> dict:
    """The profile in use (re-read when a file changes)."""
    global _cache
    stamp = tuple(os.stat(f).st_mtime if os.path.exists(f) else 0 for f in FILES)
    if _cache and _cache[0] == stamp:
        return _cache[1]
    p = dict(DEFAULTS)
    for f in FILES:
        try:
            with open(f) as fh:
                p.update(parse(fh.read()))
        except OSError:
            pass
    _cache = (stamp, p)
    return p


def audio_cpu() -> int | None:
    """The core for audio, or None when the board has no split."""
    v = profile()["AUDIO_CPU"]
    n = os.cpu_count() or 1
    if v == "last":
        return n - 1 if n > 1 else None
    return int(v) if v.isdigit() and int(v) < n and n > 1 else None


def usb_ports() -> dict[str, str]:
    return dict(x.split("=", 1) for x in profile()["USB_PORTS"].split(";") if "=" in x)
