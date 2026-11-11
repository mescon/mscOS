# mscOS

mscOS ("music OS", eller "mescon OS") är operativsystemet för **SOtM sMS-200Ultra** som nätverksspelare till **Mark Levinson Nº585.5**. Det bygger på Armbian (Debian trixie) och har tre mål:

- **Bitperfekt Spotify Lossless:** Soloist och PipeWire, verifierat i panelen.
- **Roon Bridge med rå DSD** till 585.5:an. Det kräver en egen kärnpatch.
- **Lugn hårdvara:** ljudet har en egen CPU-kärna och oanvänd hårdvara är avstängd.

| Del | Var | Vad |
|---|---|---|
| Kärna | `kernel/` | Armbian-bygge med PREEMPT och HZ=1000, plus `kernel/mscos-dsd/` (rå DSD för CM6632A) |
| System | `device/` | Trimning (CPU-delning, IRQ, DT-overlay, watchdog), Soloist, PipeWire och Roon Bridge |
| Panel | `web/` | PWA:n på `http://<enhetens namn>.local`: uppspelning, signalväg, DAC, förstärkare, källväxel, API för HA |
| Verktyg | `tools/` | Testverktyg, bland annat `dsf2u32le.py` (DSF till rå DSD_U32_LE) |
| Dokumentation | `docs/` | [Optimeringar](docs/OPTIMIZATIONS.md), [DSD](docs/DSD.md), [jämförelse med ROCK](docs/ROCK.md) |

Inställningar för den egna installationen (adresser, omvänd proxy) ligger i `/etc/mscos/site.env` på enheten, aldrig i repot.

## Vanliga kommandon

```sh
MSCOS_HOST=root@<enhet> web/deploy.sh # panelen till enheten (standard root@mscos.local)
kernel/mscos-dsd/build-module.sh       # snd-usb-audio med DSD-patchen, för den byggda kärnan
kernel/mscos-dsd/install-module.sh     # installera den (updates/), --remove för originalet
device/tune-system.sh                  # körs på enheten: systemtrimning
```

## Regler

- Inga hemligheter i repot. Spotify-nyckeln finns bara i `.env` och i `/etc/soloist.env` på enheten.
- Kärnpaketen är spärrade (`apt-mark hold`). En ny kärna kräver att DSD-modulen byggs om, se [DSD](docs/DSD.md).
- Panelen ska bara nås via LAN eller VPN.
