"""Mark Levinson Nº585 / Nº585.5 control over IP: telnet to TCP 7701, same ASCII codes as RS-232.

Requires on the amplifier: Setup → Advanced → Connect = Ethernet, and Standby mode Normal
(Green turns the network off in standby, so the amplifier could not be woken).

Codes from the "№585/№585.5 RS-232 Guide": !1038 out of standby, !1039 into standby,
!103A..!1044 inputs 1..11, !2XXX direct volume in 0.1 dB (000-800), !2GET reads it,
!1050/!1051 mute on/off. Unknown commands answer "EH?".

The guide does not document reply framing or whether the unit pushes changes, so every byte
received is kept in `rx_log` (shown in the panel), and replies are parsed leniently.
"""
from __future__ import annotations

import asyncio
import collections
import ipaddress
import logging
import re
import socket
import time

PORT = 7701
EOL = b"\r"

INPUTS = ["XLR", "RCA1", "RCA2", "RCA3", "Phono", "AES/EBU", "Coax1", "Coax2", "Opt1", "Opt2", "USB"]
INPUT_CODE = {name: f"!10{0x3A + i:02X}" for i, name in enumerate(INPUTS)}  # !103A .. !1044
USB_INPUT = "USB"

log = logging.getLogger("levinson")


class LevinsonError(RuntimeError):
    pass


class Levinson:
    def __init__(self, host: str | None = None, timeout: float = 2.0):
        self.host = host
        self.timeout = timeout
        self._r: asyncio.StreamReader | None = None
        self._w: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()
        self._reader_task: asyncio.Task | None = None
        self._lines: asyncio.Queue[str] = asyncio.Queue()
        self.rx_log: collections.deque = collections.deque(maxlen=60)  # [t, "tx"/"rx", text]
        # What we know. The protocol cannot report power or input, so those are what we last set.
        self.volume_db: float | None = None
        self.volume_t = 0.0
        self.power: bool | None = None
        self.input: str | None = None
        self.muted: bool | None = None

    # ------------------------------------------------------------- connection
    @property
    def connected(self) -> bool:
        return self._w is not None and not self._w.is_closing()

    async def _connect(self):
        if self.connected:
            return
        if not self.host:
            raise LevinsonError("ingen adress")
        try:
            self._r, self._w = await asyncio.wait_for(asyncio.open_connection(self.host, PORT), self.timeout)
        except (OSError, asyncio.TimeoutError) as e:
            self._r = self._w = None
            raise LevinsonError(f"ingen kontakt med {self.host}:{PORT} ({e})") from e
        # A powered-off amplifier or a pulled cable sends no FIN: detect it within ~10 s
        # instead of TCP's minutes of retransmission, so commands are not silently lost.
        sock = self._w.get_extra_info("socket")
        if sock is not None:
            for opt, val in ((socket.SO_KEEPALIVE, 1),):
                sock.setsockopt(socket.SOL_SOCKET, opt, val)
            for name, val in (("TCP_KEEPIDLE", 5), ("TCP_KEEPINTVL", 2), ("TCP_KEEPCNT", 3), ("TCP_USER_TIMEOUT", 8000)):
                if hasattr(socket, name):
                    sock.setsockopt(socket.IPPROTO_TCP, getattr(socket, name), val)
        self._unanswered = 0
        self._lines = asyncio.Queue()
        self._reader_task = asyncio.create_task(self._reader(self._r))

    async def close(self):
        if self._w:
            self._w.close()
        self._r = self._w = None
        if self._reader_task:
            self._reader_task.cancel()

    async def _reader(self, r: asyncio.StreamReader):
        buf = b""
        try:
            while True:
                chunk = await r.read(256)
                if not chunk:
                    break
                buf += chunk
                # strip telnet negotiation (IAC ...) if the unit sends any
                buf = re.sub(rb"\xff[\xfb-\xfe].", b"", buf)
                parts = re.split(rb"[\r\n]+", buf)
                buf = parts.pop()
                for p in parts:
                    if p.strip():
                        self._got(p.decode(errors="replace").strip())
                if buf and len(buf) > 512:
                    self._got(buf.decode(errors="replace")); buf = b""
        except (OSError, asyncio.CancelledError):
            pass
        finally:
            if buf.strip():
                self._got(buf.decode(errors="replace").strip())
            if self._w:
                self._w.close()
            self._r = self._w = None

    def _got(self, line: str):
        self.rx_log.append([time.time(), "rx", line])
        log.info("585.5 → %r", line)
        m = re.search(r"2(\d{3})\b", line)  # volume answer, e.g. "!2400" or "2400"
        if m and "GET" not in line:
            v = int(m.group(1))
            if 0 <= v <= 800:
                self.volume_db, self.volume_t = v / 10, time.time()
                self.power = True  # it answered with a level, so it is on
        self._lines.put_nowait(line)

    async def send(self, code: str, expect: str | None = None, wait: float = 1.2) -> str | None:
        """Send one code; optionally wait for a reply line matching the regex `expect`."""
        async with self._lock:
            await self._connect()
            while not self._lines.empty():
                self._lines.get_nowait()
            self.rx_log.append([time.time(), "tx", code])
            try:
                self._w.write(code.encode() + EOL)
                await self._w.drain()
            except OSError as e:
                await self.close()
                raise LevinsonError(f"sändningen misslyckades ({e})") from e
            if not expect:
                return None
            end = time.monotonic() + wait
            while (left := end - time.monotonic()) > 0:
                try:
                    line = await asyncio.wait_for(self._lines.get(), left)
                except asyncio.TimeoutError:
                    break
                if line.strip().upper() == "EH?":
                    raise LevinsonError(f"förstärkaren känner inte igen {code}")
                if re.search(expect, line):
                    return line
            return None

    # ------------------------------------------------------------- commands
    async def read_volume(self) -> float | None:
        line = await self.send("!2GET", expect=r"2\d{3}")
        return self.volume_db if line else None

    async def set_power(self, on: bool):
        await self.send("!1038" if on else "!1039")
        self.power = on

    async def set_input(self, name: str):
        if name not in INPUT_CODE:
            raise LevinsonError(f"okänd ingång {name}")
        await self.send(INPUT_CODE[name])
        self.input, self.power = name, True

    async def set_volume(self, db: float, limit: float):
        db = max(0.0, min(float(limit), float(db)))
        await self.send(f"!2{round(db * 10):03d}")
        self.volume_db, self.volume_t = round(db, 1), time.time()

    async def set_mute(self, on: bool):
        await self.send("!1050" if on else "!1051")
        self.muted = on

    async def status(self) -> dict:
        """Poll what can be polled (volume). No reply while connected suggests standby."""
        try:
            answered = await self.read_volume() is not None
        except LevinsonError as e:
            return {"reachable": False, "host": self.host, "error": str(e), **self._known()}
        self._unanswered = 0 if answered else getattr(self, "_unanswered", 0) + 1
        if self._unanswered >= 3:
            await self.close()  # reconnect next time; a half-dead socket would swallow commands
        # In standby the control port stays up (Normal standby) but the volume cannot be read.
        # Assumption to confirm on the real unit: see rx_log.
        if not answered and time.time() - self.volume_t > 25:
            self.power = False
        return {"reachable": True, "host": self.host, "answers": answered, **self._known()}

    def _known(self) -> dict:
        return {"power": self.power, "input": self.input, "volume_db": self.volume_db,
                "volume_fresh": time.time() - self.volume_t < 30, "muted": self.muted,
                "log": list(self.rx_log)[-20:]}


async def discover(cidr: str = "192.168.1.0/24", skip: set[str] = frozenset(), timeout: float = 0.6) -> str | None:
    """Find a host with TCP 7701 open on the LAN (the 585.5 gets its address over DHCP)."""
    sem = asyncio.Semaphore(64)

    async def probe(ip: str):
        async with sem:
            try:
                _, w = await asyncio.wait_for(asyncio.open_connection(ip, PORT), timeout)
                w.close()
                return ip
            except (OSError, asyncio.TimeoutError):
                return None

    hosts = [str(h) for h in ipaddress.ip_network(cidr).hosts() if str(h) not in skip]
    found = [ip for ip in await asyncio.gather(*(probe(h) for h in hosts)) if ip]
    return found[0] if found else None
