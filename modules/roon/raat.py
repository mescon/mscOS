"""What Roon Bridge says in its own log about the stream it plays.

Roon Bridge opens the DAC itself and tells the panel nothing. Its log does: for every stream
the Roon server sets up, the format it asked for, how the audio travels over the network, the
format the DAC was opened in and the size of the buffer. Read from the end of the log, a small
piece at a time, and only again when the log has grown."""
from __future__ import annotations

import json
import os
import re

LOG = "/var/roon/RAATServer/Logs/RAATServer_log.txt"
TAIL = 128 * 1024        # the latest stream is near the end; a setup is a few hundred bytes

_SETUP = re.compile(r'GOT \[\d+\] (\{"request":"setup".*\})\s*$')
_HW = re.compile(r"using hw pcmformat (\S+) bitspersample (\d+)")
_BUF = re.compile(r"Initialized with (\d+) periods with (\d+) frames/period and (\d+) frames/buffer")
_END = re.compile(r'"request":"(teardown|end_stream|stop)"')
_STAMP = re.compile(r"^(\d\d/\d\d \d\d:\d\d:\d\d) ")


def parse(text: str) -> dict:
    """The latest stream in this piece of the log: {} when none was set up in it, else
    {"type": "pcm" | "dsd", "rate", "bits", "channels", "subtype", "wire": the codec on the
    network, "hw_format", "hw_bits": the bits that carry sound in it, "period", "buffer"
    (frames), "realtime": the playing thread got realtime priority, "open": not torn down
    since, "at": the log's own stamp of the setup}."""
    lines = text.splitlines()
    at = next((i for i in range(len(lines) - 1, -1, -1) if '"request":"setup"' in lines[i]), -1)
    if at < 0:
        return {}
    m = _SETUP.search(lines[at])
    try:
        req = json.loads(m.group(1)) if m else {}
    except ValueError:
        req = {}
    f = req.get("format") if isinstance(req.get("format"), dict) else {}
    num = lambda v: v if isinstance(v, int) and not isinstance(v, bool) and 0 < v < 100_000_000 else None
    word = lambda v: v if isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9_.-]{1,24}", v) else ""
    stamp = _STAMP.match(lines[at])
    out = {"type": word(f.get("sample_type")), "rate": num(f.get("sample_rate")), "bits": num(f.get("bits_per_sample")),
           "channels": num(f.get("channels")), "subtype": word(f.get("sample_subtype")), "wire": word(req.get("wire_codec")),
           "hw_format": "", "hw_bits": None, "period": None, "buffer": None, "realtime": False, "open": True,
           "at": stamp.group(1) if stamp else ""}
    for line in lines[at + 1:]:
        if h := _HW.search(line):
            out["hw_format"], out["hw_bits"] = word(h.group(1)), int(h.group(2))
        elif b := _BUF.search(line):
            out["period"], out["buffer"] = int(b.group(2)), int(b.group(3))
        elif "sched_setscheduler succeeded" in line:
            out["realtime"] = True
        elif _END.search(line):
            out["open"] = False
    return out


class Log:
    """The log's latest stream, read again only when the file has changed."""

    def __init__(self, path: str = LOG):
        self.path, self._seen, self._value = path, None, {}

    def latest(self) -> dict:
        try:
            st = os.stat(self.path)
            seen = (st.st_size, st.st_mtime_ns, st.st_ino)
            if seen != self._seen:
                with open(self.path, "rb") as f:
                    f.seek(max(0, st.st_size - TAIL))
                    self._value = parse(f.read().decode("utf-8", "replace"))
                self._seen = seen
        except OSError:
            self._seen, self._value = None, {}
        return self._value
