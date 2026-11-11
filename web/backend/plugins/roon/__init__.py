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

    async def installed(self) -> bool:
        return os.path.exists("/opt/RoonBridge/start.sh") and os.path.exists("/etc/systemd/system/roonbridge.service")


PLUGIN = Roon
