"""UPnP renderer: this device as an output for UPnP and OpenHome apps, which is how Qobuz,
Tidal and a library on the network are played here without Roon or HQPlayer. The app (mconnect,
BubbleUPnP, USB Audio Player Pro, Linn Kazoo, ...) holds the account with the service and
sends the device the stream; the device plays it as it is.

Two programs do it, both installed on the device by the install script: MPD plays (from
Debian) and upmpdcli is the UPnP face in front of it (from its author's package repository).
MPD opens the DAC itself, like Roon Bridge does: every sample rate the DAC takes and native
DSD, nothing resampled, no software volume.

What plays comes from MPD (upmpdcli gives it the titles the app sent); the sleeve comes from
upmpdcli, asked the way an app would ask it.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import socket
import time
from xml.etree import ElementTree

from aiohttp import ClientSession, ClientTimeout

import collect
from i18n import T
from plugins import SourcePlugin

log = logging.getLogger("upnp")
MPD = ("127.0.0.1", 6600)
MPD_CONF, UPMPD_CONF = "/etc/mscos/mpd.conf", "/etc/mscos/upmpdcli.conf"
DROPINS = {"/etc/systemd/system/mpd.service.d/30-mscos.conf": f"[Service]\nExecStart=\nExecStart=/usr/bin/mpd --systemd {MPD_CONF}\n",
           "/etc/systemd/system/upmpdcli.service.d/30-mscos.conf": f"[Service]\nExecStart=\nExecStart=/usr/bin/upmpdcli -c {UPMPD_CONF}\n"}
AVT = "urn:schemas-upnp-org:service:AVTransport:1"
CODECS = {"flac": "flac", "wav": "wav", "aiff": "aiff", "dsf": "dsd", "dff": "dsd", "mp3": "mp3", "m4a": "aac", "aac": "aac", "ogg": "vorbis"}


def mpd_conf(card_id: str) -> str:
    """MPD as a plain transport: the DAC opened directly, nothing converted, no volume."""
    return f'''# Written by mscOS (the UPnP renderer module). Changes are overwritten.
user "mpd"
playlist_directory "/var/lib/mpd/playlists"
bind_to_address "127.0.0.1"
port "6600"
zeroconf_enabled "no"
auto_update "no"
replaygain "off"
volume_normalization "no"
input {{
    plugin "curl"
}}
audio_output {{
    type "alsa"
    name "DAC"
    device "hw:CARD={card_id},DEV=0"
    mixer_type "none"
    auto_resample "no"
    auto_format "no"
    auto_channels "no"
    dop "no"
}}
'''


def upmpdcli_conf(name: str) -> str:
    name = " ".join(name.split())
    return f'''# Written by mscOS (the UPnP renderer module). Changes are overwritten.
friendlyname = {name}
avfriendlyname = {name}
mpdhost = {MPD[0]}
mpdport = {MPD[1]}
upnpav = 1
openhome = 1
ownqueue = 1
checkcontentformat = 1
'''


def didl(xml: str) -> dict:
    """Title, artist, album and sleeve from the DIDL-Lite an app sent with the track."""
    out = {}
    # It comes from whichever app on the network sent the track: no document type and no
    # entities of its own (nothing to expand or fetch), and not more than a track's worth.
    if len(xml) > 65536 or "<!" in xml:
        return out
    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return out
    for el in root.iter():
        tag, text = el.tag.rsplit("}", 1)[-1], (el.text or "").strip()
        key = {"title": "title", "artist": "artist", "creator": "artist", "album": "album", "albumArtURI": "image"}.get(tag)
        if key and text and key not in out:
            out[key] = text
    if not re.match(r"https?://[^\s\"'<>]+$", out.get("image", "")):
        out.pop("image", None)
    return out


class Upnp(SourcePlugin):
    id = "upnp"
    label = "UPnP"
    protocol = "UPnP"
    pipewire = False
    name = {"sv": "UPnP-mottagare", "en": "UPnP renderer"}
    description = {"sv": "Enheten som utgång för UPnP- och OpenHome-appar: Qobuz, Tidal och egna filer från t.ex. mconnect eller BubbleUPnP. Alla format DAC:en klarar, även DSD.",
                   "en": "This device as an output for UPnP and OpenHome apps: Qobuz, Tidal and your own files from e.g. mconnect or BubbleUPnP. Every format the DAC takes, DSD too."}
    system_units = ("mpd.service", "upmpdcli.service")
    hint = {"sv": "Välj {name} som utgång i din UPnP-app", "en": "Choose {name} as the output in your UPnP app"}
    terms = {"sv": "MPD hämtas från Debian och upmpdcli från dess upphovsman (lesbonscomptes.com) till den här enheten. Kontot hos Qobuz eller Tidal finns i din app, inte här.",
             "en": "MPD is downloaded from Debian and upmpdcli from its author (lesbonscomptes.com) to this device. The Qobuz or Tidal account is in your app, not here."}
    terms_url = "https://www.lesbonscomptes.com/upmpdcli/"
    transport = ("toggle", "play", "pause", "next", "prev", "seek")
    updates = "self"   # both come from apt repositories: they follow the system's updates

    def __init__(self, hub):
        super().__init__(hub)
        self.np: dict | None = None      # what plays, as now_playing() gives it
        self.control = ""                # upmpdcli's AVTransport control address, once found
        self.http: ClientSession | None = None

    # ---- files
    @staticmethod
    def _put(path: str, text: str) -> bool:
        if collect.read(path) == text:
            return False
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".new", "w", encoding="utf-8") as f:
            f.write(text)
        os.chmod(path + ".new", 0o644)
        os.replace(path + ".new", path)
        return True

    async def write_config(self) -> bool:
        """The two programs' settings for this device. True when something changed."""
        card = collect._usb_card()
        card_id = collect.read(f"{card[0]}/id").strip() if card else ""
        if not card_id:
            raise RuntimeError(T("Ingen DAC ansluten."))
        if any([self._put(p, t) for p, t in DROPINS.items()]):
            await collect.run(["systemctl", "daemon-reload"], timeout=30)
        return any([self._put(MPD_CONF, mpd_conf(card_id)), self._put(UPMPD_CONF, upmpdcli_conf(self.hub.names()["network"]))])

    # ---- source
    async def installed(self) -> bool:
        return os.path.exists("/usr/bin/mpd") and os.path.exists("/usr/bin/upmpdcli")

    async def version(self) -> str | None:
        rc, out, _ = await collect.run(["dpkg-query", "-W", "-f", "${Version}", "upmpdcli"], timeout=10)
        return re.sub(r"-.*$", "", out.strip()) or None if rc == 0 else None

    async def start(self) -> str:
        try:
            if await self.write_config() and await self.active():
                await collect.run(["systemctl", "restart", *self.system_units], timeout=60)
        except (OSError, RuntimeError) as e:
            return str(e)
        return await super().start()

    async def stop(self) -> str:
        self.np, self.control = None, ""
        return await super().stop()

    async def names_changed(self) -> None:
        if await self.installed() and await self.write_config() and await self.active():
            await collect.run(["systemctl", "restart", "upmpdcli.service"], timeout=60)
            self.control = ""

    async def playing(self) -> bool | None:
        return bool(self.np and self.np.get("playing"))

    def now_playing(self) -> dict | None:
        return self.np

    # ---- MPD
    async def _mpd(self, *commands: str) -> dict:
        """Send commands, return the "key: value" lines of the answers as one dict."""
        r, w = await asyncio.wait_for(asyncio.open_connection(*MPD), 3)
        try:
            if not (await asyncio.wait_for(r.readline(), 3)).startswith(b"OK MPD"):
                raise OSError("not MPD")
            w.write(("command_list_begin\n" + "".join(c + "\n" for c in commands) + "command_list_end\n").encode())
            await w.drain()
            out = {}
            while True:
                line = (await asyncio.wait_for(r.readline(), 5)).decode("utf-8", "replace").rstrip("\n")
                if line == "OK" or not line:
                    return out
                if line.startswith("ACK"):
                    raise RuntimeError(line)
                k, _, v = line.partition(": ")
                out[k] = v
        finally:
            w.close()

    async def command(self, cmd: str, **kw) -> None:
        try:
            if cmd == "toggle":
                await self._mpd("pause" if (self.np or {}).get("playing") else "play")
            elif cmd in ("play", "next"):
                await self._mpd(cmd)
            elif cmd == "pause":
                await self._mpd("pause 1")
            elif cmd == "prev":
                await self._mpd("previous")
            elif cmd == "seek":
                await self._mpd(f"seekcur {max(0, int(kw['position_ms'])) / 1000:.3f}")
        except (OSError, RuntimeError, asyncio.TimeoutError):
            raise RuntimeError(T("Spelaren svarar inte."))
        await self._read()

    # ---- upmpdcli, asked as an app would: where its AVTransport service is, then the track's metadata
    async def _find_control(self) -> str:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setblocking(False)
        loop = asyncio.get_running_loop()
        try:
            msg = f'M-SEARCH * HTTP/1.1\r\nHOST: 239.255.255.250:1900\r\nMAN: "ssdp:discover"\r\nMX: 1\r\nST: {AVT}\r\n\r\n'.encode()
            sock.sendto(msg, ("239.255.255.250", 1900))
            end = loop.time() + 2.5
            while loop.time() < end:
                try:
                    data, addr = await asyncio.wait_for(loop.sock_recvfrom(sock, 2048), max(0.05, end - loop.time()))
                except asyncio.TimeoutError:
                    break
                m = re.search(rb"^LOCATION:\s*(http://\S+)", data, re.I | re.M)
                if not m or not self._is_local(addr[0]):   # another renderer on the network
                    continue
                url = m.group(1).decode()
                async with self.http.get(url) as resp:
                    desc = await resp.text()
                for svc in re.findall(r"<service>(.*?)</service>", desc, re.S):
                    if AVT in svc and (c := re.search(r"<controlURL>([^<]+)</controlURL>", svc)):
                        base = re.match(r"http://[^/]+", url).group(0)
                        return base + (c.group(1) if c.group(1).startswith("/") else "/" + c.group(1))
        except OSError:
            pass
        finally:
            sock.close()
        return ""

    @staticmethod
    def _is_local(ip: str) -> bool:
        """Is this one of the device's own addresses? (Binding to it works only then.)"""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.bind((ip, 0))
            return True
        except OSError:
            return False
        finally:
            s.close()

    async def _sent_metadata(self) -> dict:
        if self.http is None:
            self.http = ClientSession(timeout=ClientTimeout(total=4))
        if not self.control:
            self.control = await self._find_control()
        if not self.control:
            return {}
        body = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                's:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/"><s:Body>'
                f'<u:GetPositionInfo xmlns:u="{AVT}"><InstanceID>0</InstanceID></u:GetPositionInfo></s:Body></s:Envelope>')
        try:
            async with self.http.post(self.control, data=body.encode(), headers={
                    "Content-Type": 'text/xml; charset="utf-8"', "SOAPACTION": f'"{AVT}#GetPositionInfo"'}) as r:
                text = await r.text()
            m = re.search(r"<TrackMetaData>(.*?)</TrackMetaData>", text, re.S)
            if not m:
                return {}
            inner = m.group(1).replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&apos;", "'").replace("&amp;", "&")
            return didl(inner)
        except (OSError, asyncio.TimeoutError):
            self.control = ""
            return {}

    # ---- follow while it is the source
    async def _read(self) -> None:
        st = await self._mpd("status", "currentsong")
        state = st.get("state", "stop")
        if state == "stop" or not st.get("file"):
            np = None
        else:
            sent = getattr(self, "_sent", (None, {}))
            if sent[0] != st.get("file"):      # another track: what did the app say about it?
                sent = self._sent = (st.get("file"), await self._sent_metadata())
            meta = sent[1]
            rate, bits, _ = (st.get("audio", "::").split(":") + ["", ""])[:3]
            ext = re.sub(r"\?.*$", "", st.get("file", "")).rsplit(".", 1)[-1].lower()
            np = {"title": st.get("Title") or meta.get("title") or st.get("Name") or "",
                  "artist": st.get("Artist") or meta.get("artist") or "",
                  "album": st.get("Album") or meta.get("album") or "",
                  "image": meta.get("image"), "playing": state == "play",
                  "duration_ms": int(float(st["duration"]) * 1000) if st.get("duration") else None,
                  "position_ms": int(float(st.get("elapsed", 0)) * 1000), "position_ts": time.time(),
                  "codec": CODECS.get(ext) or ("dsd" if rate.startswith("dsd") else None),
                  "bitrate": int(st["bitrate"]) if st.get("bitrate", "0") not in ("", "0") else None,
                  "shuffle": st.get("random") == "1", "repeat": "track" if st.get("single") == "1" and st.get("repeat") == "1" else "context" if st.get("repeat") == "1" else "off"}
        changed = (np is None) != (self.np is None) or any((np or {}).get(k) != (self.np or {}).get(k) for k in ("title", "artist", "album", "image", "playing", "duration_ms"))
        seeked = np and self.np and abs((np["position_ms"] - self.np["position_ms"]) - (np["position_ts"] - self.np["position_ts"]) * 1000 * (1 if np["playing"] else 0)) > 2500
        self.np = np
        if changed or seeked:
            self.hub.source_changed(self)

    async def _follow(self):
        while True:
            if self.hub.settings.get("source") != self.id:
                self.np = None
                await asyncio.sleep(5)
                continue
            try:
                await self._read()
            except (OSError, RuntimeError, asyncio.TimeoutError, ValueError):
                if self.np is not None:
                    self.np = None
                    self.hub.source_changed(self)
                await asyncio.sleep(4)
                continue
            except Exception:  # noqa: BLE001 - never let this loop die
                log.exception("following MPD")
            await asyncio.sleep(1.5)

    def tasks(self) -> list:
        return [self._follow()]


PLUGIN = Upnp
