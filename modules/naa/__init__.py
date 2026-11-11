"""HQPlayer NAA (Network Audio Adapter): this device as the output of Signalyst HQPlayer.
HQPlayer does the processing on another machine and sends the finished stream here; the
NAA daemon opens the DAC itself (PCM and DSD). The daemon is proprietary: the install
script downloads it from Signalyst on the device, after the user has accepted its terms."""
from __future__ import annotations

import os

from plugins import SourcePlugin


class Naa(SourcePlugin):
    id = "naa"
    label = "HQPlayer"
    protocol = "NAA"
    pipewire = False
    name = {"sv": "HQPlayer NAA", "en": "HQPlayer NAA"}
    description = {"sv": "Enheten som nätverksutgång för HQPlayer (Qobuz, Tidal och egna filer via HQPlayer, även DSD).",
                   "en": "This device as the network output of HQPlayer (Qobuz, Tidal and your own files through HQPlayer, DSD too)."}
    system_units = ("networkaudiod.service",)
    hint = {"sv": "Välj den här enheten som utgång i HQPlayer", "en": "Choose this device as the output in HQPlayer"}
    terms = {"sv": "NAA-programmet hämtas från Signalyst till den här enheten. Det får bara användas med en giltig HQPlayer-licens, och genom att installera godkänner du Signalysts licensavtal.",
             "en": "The NAA daemon is downloaded from Signalyst to this device. It may only be used with a valid HQPlayer licence, and by installing you accept Signalyst's licence agreement."}
    terms_url = "https://signalyst.com/network-audio/"

    INDEX = "https://www.signalyst.eu/bins/naa/linux/trixie/"

    async def version(self) -> str | None:
        import collect
        rc, out, _ = await collect.run(["dpkg-query", "-W", "-f", "${Version}", "networkaudiod"], timeout=10)
        return out.strip() or None if rc == 0 else None

    async def latest(self) -> str | None:
        """The newest package for this architecture in Signalyst's directory."""
        import platform
        import re
        from aiohttp import ClientSession, ClientTimeout
        arch = {"armv7l": "armhf", "aarch64": "arm64", "x86_64": "amd64"}.get(platform.machine())
        if not arch:
            return None
        async with ClientSession(timeout=ClientTimeout(total=15)) as http, http.get(self.INDEX) as r:
            found = set(re.findall(rf"networkaudiod_([0-9.]+-[0-9]+)_{arch}\.deb", await r.text()))
        key = lambda v: [int(x) for x in re.split(r"[.-]", v)]  # noqa: E731
        return max(found, key=key) if found else None

    async def installed(self) -> bool:
        return os.path.exists("/usr/sbin/networkaudiod")


PLUGIN = Naa
