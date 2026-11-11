"""Native DSD for DACs with a C-Media CM6632A USB receiver that takes raw DSD in a 64-bit
subslot (Mark Levinson Nº585 and Nº585.5). Linux has no support for that format; the
mscOS kernel patch (kernel/mscos-dsd) adds it to snd-usb-audio, so players that open the
DAC themselves (Roon Bridge, HQPlayer NAA) can send DSD64 and DSD128 natively.

mscOS images carry the patch in their kernel. On another kernel the driver is built on the
device by the mscos-dsd-dkms package. This module shows whether the connected DAC is one
of these, whether the running driver has the patch, and whether DSD is on offer.
"""
from __future__ import annotations

import glob

import collect
from plugins import Plugin

IDS = {"0ecb:18a7": "Mark Levinson Nº585 / Nº585.5"}
PARAM = "/sys/module/snd_usb_audio/parameters/mscos_dsd"


class Cm6632aDsd(Plugin):
    id = "cm6632a"
    kind = "device"
    name = {"sv": "Nativ DSD för CM6632A", "en": "Native DSD for CM6632A"}
    description = {"sv": "DSD64 och DSD128 utan omvandling till DAC:ar med C-Media CM6632A (Mark Levinson Nº585, Nº585.5). För Roon och HQPlayer.",
                   "en": "DSD64 and DSD128 without conversion to DACs with a C-Media CM6632A (Mark Levinson Nº585, Nº585.5). For Roon and HQPlayer."}

    def state(self) -> dict:
        ids = {collect.read(f).strip() for f in glob.glob("/proc/asound/card*/usbid")}
        dac = next((IDS[i] for i in ids if i in IDS), None)
        driver = collect.read(PARAM).strip() in ("Y", "1")
        offered = any("DSD_U32" in collect.read(f) for f in glob.glob("/proc/asound/card*/stream0"))
        return {"dac": dac, "driver": driver, "offered": offered}

    def status(self) -> dict:
        s = self.state()
        if s["offered"]:
            return {"state": "ok", "text": {"sv": f"{s['dac'] or 'DAC:n'} tar emot nativ DSD.",
                                            "en": f"{s['dac'] or 'The DAC'} takes native DSD."}}
        if not s["driver"]:
            return {"state": "warn", "text": {
                "sv": "Kärnans ljuddrivrutin saknar DSD-tillägget. Installera paketet mscos-dsd-dkms, eller använd en mscOS-avbildning.",
                "en": "The kernel's audio driver lacks the DSD addition. Install the mscos-dsd-dkms package, or use an mscOS image."}}
        if not s["dac"]:
            return {"state": "idle", "text": {"sv": "Drivrutinen är på plats. Ingen DAC med CM6632A är ansluten.",
                                              "en": "The driver is in place. No DAC with a CM6632A is connected."}}
        return {"state": "warn", "text": {"sv": "DAC:n är ansluten men erbjuder inte DSD. Den behöver USB 2.0 (high speed).",
                                          "en": "The DAC is connected but does not offer DSD. It needs USB 2.0 (high speed)."}}

    def features(self) -> dict:
        return {"dsd_native": self.state()["offered"]}


PLUGIN = Cm6632aDsd
