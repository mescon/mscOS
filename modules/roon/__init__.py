"""Roon Bridge: this device as a Roon endpoint (RAAT). Roon opens the DAC itself, which
is what lets it send DSD and every sample rate the DAC takes. Installed on the device
from Roon Labs by the install script."""
from __future__ import annotations

import os

from plugins import SourcePlugin


class Roon(SourcePlugin):
    id = "roon"
    label = "Roon"
    protocol = "RAAT"
    pipewire = False
    name = {"sv": "Roon Bridge", "en": "Roon Bridge"}
    description = {"sv": "Enheten som Roon-utgång. Alla format DAC:en klarar, även DSD. Kräver en Roon-server i nätverket.",
                   "en": "This device as a Roon output. Every format the DAC takes, DSD too. Needs a Roon server on the network."}
    system_units = ("roonbridge.service",)
    hint = {"sv": "Välj den här enheten i Roon-appen", "en": "Choose this device in the Roon app"}
    terms = {"sv": "Roon Bridge hämtas från Roon Labs till den här enheten.",
             "en": "Roon Bridge is downloaded from Roon Labs to this device."}
    terms_url = "https://help.roonlabs.com/portal/en/kb/articles/linux-install"

    updates = "self"  # Roon Bridge updates itself from the Roon server

    async def version(self) -> str | None:
        import collect
        lines = collect.read("/opt/RoonBridge/VERSION").splitlines()
        return (lines[1].split(" production")[0].strip() if len(lines) > 1 else "") or None

    NAME_FILE = "/etc/mscos/roon-name"
    DROPIN = "/etc/systemd/system/roonbridge.service.d/30-name.conf"

    @staticmethod
    def _put(path: str, text: str) -> bool:
        """Write the file when it differs. True when it was written."""
        import collect
        if collect.read(path) == text:
            return False
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".new", "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(path + ".new", path)
        return True

    async def write_name(self) -> bool:
        """Roon heads the device with the machine's host name: roon-start gives Roon Bridge
        the device's name on the network instead. True when the name changed."""
        import collect
        starter = os.path.join(self.module_dir, "roon-start")
        unit = f"[Service]\nExecStart=\nExecStart=/usr/bin/python3 {starter}\n"
        if self._put(self.DROPIN, unit):
            await collect.run(["systemctl", "daemon-reload"], timeout=30)
        return self._put(self.NAME_FILE, self.hub.names()["network"] + "\n")

    async def start(self) -> str:
        try:
            await self.write_name()
        except OSError as e:
            return str(e)
        return await super().start()

    async def names_changed(self) -> None:
        import collect
        if await self.installed() and await self.write_name() and await self.active():
            await collect.run(["systemctl", "restart", *self.system_units], timeout=60)

    async def installed(self) -> bool:
        return os.path.exists("/opt/RoonBridge/start.sh") and os.path.exists("/etc/systemd/system/roonbridge.service")


PLUGIN = Roon
