"""Arcam AVR10/20/30 IP control (TCP 50000), protocol SH289E.

Frame:    0x21 Zone Cc Dl Data... 0x0D
Response: 0x21 Zone Cc Ac Dl Data... 0x0D   (Ac 0x00 = OK)
"""
from __future__ import annotations

import asyncio

PORT = 50000
ZONE = 0x01

# 0x1D "request current source" answer codes.
SOURCES = {
    0x01: "CD", 0x02: "BD", 0x03: "AV", 0x04: "SAT", 0x05: "PVR", 0x06: "UHD",
    0x08: "AUX", 0x09: "Display", 0x0B: "FM", 0x0C: "DAB", 0x0E: "NET",
    0x10: "STB", 0x11: "Game", 0x12: "BT",
}
# RC5 (system 16) codes that select each source.
SOURCE_RC5 = {
    "CD": 118, "BD": 98, "AV": 94, "SAT": 27, "PVR": 96, "UHD": 125, "AUX": 99,
    "FM": 28, "DAB": 72, "NET": 92, "STB": 100, "Game": 97, "BT": 122,
}
RC5_POWER_ON, RC5_POWER_OFF = 123, 124
RC5_MUTE_ON, RC5_MUTE_OFF = 26, 120


class ArcamError(RuntimeError):
    pass


class Arcam:
    def __init__(self, host: str, timeout: float = 2.5):
        self.host = host
        self.timeout = timeout
        self._lock = asyncio.Lock()

    async def _xfer(self, cc: int, data: bytes) -> bytes:
        """Send one command, return the response data bytes (raises on error/timeout)."""
        async with self._lock:
            try:
                r, w = await asyncio.wait_for(asyncio.open_connection(self.host, PORT), self.timeout)
            except (OSError, asyncio.TimeoutError) as e:
                raise ArcamError(f"no connection to {self.host}:{PORT} ({e})") from e
            try:
                w.write(bytes([0x21, ZONE, cc, len(data)]) + data + b"\x0d")
                await w.drain()
                buf = b""
                while True:
                    chunk = await asyncio.wait_for(r.read(64), self.timeout)
                    if not chunk:
                        break
                    buf += chunk
                    # A complete frame for our command code?
                    i = buf.find(bytes([0x21, ZONE, cc]))
                    if i >= 0 and len(buf) >= i + 5:
                        dl = buf[i + 4]
                        end = i + 5 + dl
                        if len(buf) > end and buf[end] == 0x0D:
                            ac = buf[i + 3]
                            if ac != 0x00:
                                raise ArcamError(f"command 0x{cc:02x} answer code 0x{ac:02x}")
                            return buf[i + 5:end]
            except asyncio.TimeoutError as e:
                raise ArcamError("timeout waiting for answer") from e
            finally:
                w.close()
            raise ArcamError("connection closed without answer")

    async def rc5(self, command: int, system: int = 16) -> None:
        await self._xfer(0x08, bytes([system, command]))

    async def status(self) -> dict:
        power = (await self._xfer(0x00, b"\xf0"))[0] == 0x01
        out = {"reachable": True, "power": power}
        if power:
            src = (await self._xfer(0x1D, b"\xf0"))[0]
            out["source"] = SOURCES.get(src, f"0x{src:02x}")
            out["volume"] = (await self._xfer(0x0D, b"\xf0"))[0]
            out["muted"] = (await self._xfer(0x0E, b"\xf0"))[0] == 0x00
        return out

    async def set_power(self, on: bool) -> None:
        await self.rc5(RC5_POWER_ON if on else RC5_POWER_OFF)

    async def set_source(self, name: str) -> None:
        if name not in SOURCE_RC5:
            raise ArcamError(f"unknown source {name}")
        await self.rc5(SOURCE_RC5[name])

    async def set_volume(self, vol: int) -> None:
        vol = max(0, min(99, int(vol)))
        await self._xfer(0x0D, bytes([vol]))

    async def set_mute(self, muted: bool) -> None:
        await self.rc5(RC5_MUTE_ON if muted else RC5_MUTE_OFF)
