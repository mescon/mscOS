"""A small client for the Roon extension API: what a zone plays, and its transport.

Roon Bridge only receives audio: it is told nothing about the music. A Roon server does tell
its extensions, over a WebSocket on the server (the "MOO" messages below). An extension
announces itself; the listener approves it once in Roon (Settings, Extensions); from then
on the server sends every zone's state: title, artist, album, a key for the sleeve, position.

The protocol is Roon Labs' own, as their node-roon-api speaks it:
  discovery   a "SOOD" query over UDP (multicast and broadcast, port 9003); servers answer
              with their address and HTTP port
  messages    one per binary WebSocket frame:  MOO/1 <VERB> <name>\\n  headers  \\n  body
              REQUEST service/method, answered by CONTINUE (a subscription's updates) or COMPLETE
  services    com.roonlabs.registry:1 (register), com.roonlabs.transport:2 (zones, control),
              and com.roonlabs.ping:1, which the extension itself answers
"""
from __future__ import annotations

import asyncio
import json
import socket
import uuid

SOOD_PORT, SOOD_GROUP = 9003, "239.255.90.90"
ROON_SERVICE = "00720724-5143-4a9b-abac-0e50cba674bb"
TRANSPORT = "com.roonlabs.transport:2"


# ---- discovery
def sood_query(tid: str) -> bytes:
    out = b"SOOD\x02Q"
    for k, v in (("query_service_id", ROON_SERVICE), ("_tid", tid)):
        kb, vb = k.encode(), v.encode()
        out += bytes([len(kb)]) + kb + len(vb).to_bytes(2, "big") + vb
    return out


def sood_parse(data: bytes) -> dict | None:
    """The properties of a SOOD message, or None when it is not one."""
    if len(data) < 6 or data[:4] != b"SOOD" or data[4] != 2:
        return None
    props, pos = {"_type": chr(data[5])}, 6
    try:
        while pos < len(data):
            n = data[pos]; pos += 1
            if n == 0 or pos + n + 2 > len(data):
                return None
            name = data[pos:pos + n].decode(); pos += n
            ln = int.from_bytes(data[pos:pos + 2], "big"); pos += 2
            if ln == 0xFFFF:
                props[name] = None
            else:
                if pos + ln > len(data):
                    return None
                props[name] = data[pos:pos + ln].decode(); pos += ln
    except UnicodeDecodeError:
        return None
    return props


async def discover(wait: float = 2.5) -> list[dict]:
    """The Roon servers on the network: [{"host", "port", "id", "name"}]."""
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    found: dict[str, dict] = {}
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        sock.setblocking(False)
        sock.bind(("", 0))
        q = sood_query(str(uuid.uuid4()))
        for dest in (SOOD_GROUP, "255.255.255.255"):
            try:
                sock.sendto(q, (dest, SOOD_PORT))
            except OSError:
                pass
        end = loop.time() + wait
        while loop.time() < end:
            try:
                data, addr = await asyncio.wait_for(loop.sock_recvfrom(sock, 4096), max(0.05, end - loop.time()))
            except asyncio.TimeoutError:
                break
            p = sood_parse(data)
            if not p or p.get("service_id") != ROON_SERVICE or not p.get("unique_id") or not str(p.get("http_port", "")).isdigit():
                continue
            found[p["unique_id"]] = {"host": p.get("_replyaddr") or addr[0], "port": int(p["http_port"]),
                                     "id": p["unique_id"], "name": p.get("name") or ""}
    except OSError:
        pass
    finally:
        sock.close()
    return list(found.values())


# ---- messages
def moo_encode(verb: str, name: str, request_id, body=None) -> bytes:
    head = f"MOO/1 {verb} {name}\nRequest-Id: {request_id}\n"
    if body is None:
        return (head + "\n").encode()
    data = json.dumps(body).encode()
    return (head + f"Content-Length: {len(data)}\nContent-Type: application/json\n\n").encode() + data


def moo_parse(frame: bytes) -> dict | None:
    """{"verb", "name", "request_id", "body"} (name is "service/method" for a REQUEST), or None."""
    head, sep, rest = frame.partition(b"\n\n")
    if not sep:
        return None
    lines = head.decode("utf-8", "replace").split("\n")
    first = lines[0].split(" ", 2)
    if len(first) != 3 or first[0] != "MOO/1":
        return None
    msg = {"verb": first[1], "name": first[2], "request_id": None, "body": None}
    ctype, clen = "", None
    for line in lines[1:]:
        k, _, v = line.partition(":")
        k, v = k.strip(), v.strip()
        if k == "Request-Id":
            msg["request_id"] = v
        elif k == "Content-Type":
            ctype = v
        elif k == "Content-Length" and v.isdigit():
            clen = int(v)
    if msg["request_id"] is None:
        return None
    if clen and ctype == "application/json":
        try:
            msg["body"] = json.loads(rest[:clen])
        except ValueError:
            return None
    return msg


class Roon:
    """One connection to a Roon server, as an extension. `changed()` is called whenever the
    zones or the registration change; `token` is kept by the caller between connections."""

    def __init__(self, info: dict, changed, token: str = ""):
        self.info, self.changed, self.token = info, changed, token
        self.ws = None
        self.core: dict = {}            # the server's answer to the registration; {} until approved
        self.zones: dict[str, dict] = {}
        self._id = 0
        self._waiting: dict[str, asyncio.Future] = {}

    @property
    def registered(self) -> bool:
        return bool(self.core)

    async def _send(self, name: str, body=None) -> str:
        rid = str(self._id); self._id += 1
        await self.ws.send(moo_encode("REQUEST", name, rid, body))
        return rid

    async def request(self, name: str, body=None, timeout: float = 8) -> dict:
        """A request with one answer. Raises RuntimeError when the server says no or is gone."""
        if self.ws is None:
            raise RuntimeError("not connected")
        fut = asyncio.get_running_loop().create_future()
        rid = await self._send(name, body)
        self._waiting[rid] = fut
        try:
            msg = await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            raise RuntimeError("timeout")
        finally:
            self._waiting.pop(rid, None)
        if msg["name"] != "Success":
            raise RuntimeError(msg["name"])
        return msg.get("body") or {}

    async def run(self, ws) -> None:
        """Speak with the server until the connection ends."""
        self.ws = ws
        try:
            await self._send("com.roonlabs.registry:1/info")          # request 0
            reg_id = sub_id = None
            async for frame in ws:
                msg = moo_parse(frame if isinstance(frame, bytes) else frame.encode())
                if not msg:
                    continue
                rid = msg["request_id"]
                if msg["verb"] == "REQUEST":
                    # the server asks us: only its ping; anything else we do not offer
                    ok = msg["name"] == "com.roonlabs.ping:1/ping"
                    await ws.send(moo_encode("COMPLETE", "Success" if ok else "InvalidRequest", rid))
                elif rid == "0" and reg_id is None:
                    reg_id = await self._send("com.roonlabs.registry:1/register", {**self.info, **({"token": self.token} if self.token else {})})
                    self.changed()   # asked to be let in: until the server answers, the listener has to approve
                elif rid == reg_id:
                    if msg["name"] != "Registered":
                        continue
                    self.core = msg.get("body") or {}
                    self.token = self.core.get("token") or self.token
                    self.changed()
                    if sub_id is None:
                        sub_id = await self._send(f"{TRANSPORT}/subscribe_zones", {"subscription_key": 0})
                elif rid == sub_id:
                    self._zones(msg)
                elif rid in self._waiting and not self._waiting[rid].done():
                    self._waiting[rid].set_result(msg)
        finally:
            self.ws, self.core, self.zones = None, {}, {}
            for f in self._waiting.values():
                if not f.done():
                    f.set_exception(RuntimeError("connection lost"))
            self.changed()

    def _zones(self, msg: dict) -> None:
        b = msg.get("body") or {}
        if msg["name"] == "Subscribed":
            self.zones = {z["zone_id"]: z for z in b.get("zones", []) if z.get("zone_id")}
        elif msg["name"] == "Changed":
            for zid in b.get("zones_removed", []):
                self.zones.pop(zid, None)
            for z in b.get("zones_added", []) + b.get("zones_changed", []):
                if z.get("zone_id"):
                    self.zones[z["zone_id"]] = z
            for s in b.get("zones_seek_changed", []):
                z = self.zones.get(s.get("zone_id"))
                if z is not None:
                    z["seek_position"] = s.get("seek_position")
                    if z.get("now_playing") is not None:
                        z["now_playing"]["seek_position"] = s.get("seek_position")
        self.changed()

    # ---- transport
    async def control(self, zone_id: str, what: str) -> None:
        await self.request(f"{TRANSPORT}/control", {"zone_or_output_id": zone_id, "control": what})

    async def seek(self, zone_id: str, seconds: float) -> None:
        await self.request(f"{TRANSPORT}/seek", {"zone_or_output_id": zone_id, "how": "absolute", "seconds": seconds})

    async def settings(self, zone_id: str, **kw) -> None:
        await self.request(f"{TRANSPORT}/change_settings", {"zone_or_output_id": zone_id, **kw})
