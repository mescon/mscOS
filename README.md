# mscOS

*by vivajAcoustics*

mscOS ("music OS") turns a small computer into a network music player for a USB DAC: a
bit-perfect endpoint for Spotify, Roon and HQPlayer, set up and watched from a web panel
in Swedish or English.

- **Bit-perfect, and it shows it.** The panel measures what actually reaches the DAC:
  16 or 24 bit, lossless or not, dropouts, the DAC's own clock.
- **Sources as modules.** One at a time owns the DAC:
  - Spotify Lossless (Soloist, with your own developer key),
  - Spotify Connect without a key (go-librespot, 320 kbit/s),
  - Roon Bridge,
  - HQPlayer NAA.
- **Your equipment as modules.** Amplifier volume and inputs over the network (Mark
  Levinson Nº585/585.5), an Arcam AV receiver, alerts to your phone (ntfy, Home Assistant),
  native DSD for C-Media CM6632A DACs, and modules of your own. The core needs none of
  them: any USB DAC works.
- **A quiet machine.** One CPU core for audio and the USB interrupts, no frequency
  scaling, nothing unscheduled while music plays; the panel lists each measure and checks
  it live.
- **No keys, no player binaries included.** Players are downloaded on the device from
  their publishers; keys and accounts are your own.

It is Debian 13 with the mscOS packages on top, built into images with the Armbian
framework. It started on a SOtM sMS-200ultra, which is still the only board it has been
tested on; see [status](#status).

## Install

See [docs/INSTALL.md](docs/INSTALL.md). In short: write the image to an SD card, start the
device on wired network, open `http://mscos.local` and follow the guide. On an existing
Debian 13 or Armbian system, install the packages instead.

## Status

| | State |
|---|---|
| SOtM sMS-200, sMS-200ultra (Allwinner A20) | in daily use, installed from a checkout; the image (`image/build.sh sotm`) builds and its contents are checked, but it has **not yet been started on a device** |
| x86-64 PCs | the image builds and starts in a virtual machine (first boot, board profile, kernel arguments, the guide); **not yet tested on hardware or with a DAC** |
| Raspberry Pi 4/5, other boards | profiles and image targets exist, **not yet built or tested** |
| Spotify Lossless (Soloist), Roon Bridge | in daily use on the SOtM |
| Spotify Connect (go-librespot), HQPlayer NAA | install, start and source switching verified on the SOtM; **playback not yet tested** |
| Native DSD for CM6632A | in use as a kernel patch; the DKMS package is **untested** |
| Panel in Swedish and English | complete |
| Alerts through ntfy, modules of your own, updating mscOS from the panel | written; **not yet tested against a real ntfy topic, a published module or a release** |

Planned: UPnP/OpenHome (Qobuz and Tidal through a control app), Squeezelite, AirPlay.
Tidal Connect and Qobuz Connect are only offered to certified manufacturers and are not
part of mscOS. See [docs/PLATFORM-PLAN.md](docs/PLATFORM-PLAN.md) (Swedish) for the reasoning.

## Layout

| Path | What |
|---|---|
| `web/` | the panel: `backend/` (Python, aiohttp) and `static/` (a PWA without a build step) |
| `modules/` | one directory per module: sources (with the script that downloads the player on the device), amplifiers, alerts, integrations. [Contributing a module](modules/README.md) |
| `core/` | system setup (`mscos-setup`), board detection, boot-time tuning, PipeWire configuration |
| `boards/` | board profiles: what differs between boards (audio core, interrupts, sensors) |
| `kernel/` | kernel configuration and the native-DSD patch (`mscos-dsd/`, also as DKMS) |
| `packaging/` | builds the Debian packages: `mscos-core`, `mscos-panel`, `mscos-dsd-dkms`, `mscos` |
| `image/` | builds images with the Armbian framework |
| `docs/` | [install](docs/INSTALL.md), [modules](docs/MODULES.md), and in Swedish: [optimizations](docs/OPTIMIZATIONS.md), [DSD](docs/DSD.md), [compared with ROCK](docs/ROCK.md) |

## Development

```sh
packaging/build.sh                       # the .deb packages -> packaging/out/
image/build.sh sotm                      # an image (sotm | rpi | pc | arm64)
MSCOS_HOST=root@<device> web/deploy.sh   # this checkout onto a device, at the packages' paths
tools/i18n-keys.py web/static web/static/i18n/en.json   # texts missing from a translation
```

Swedish is the source language of the panel: texts are written in Swedish in the code and
`web/static/i18n/<language>.json` maps them to another language. To add a language, copy
`en.json`, translate the values and add the language to `LANGS` in `web/static/i18n.js` and
`web/backend/i18n.py`.

A module is a directory in `modules/`; see [modules/README.md](modules/README.md) for how to contribute one and [docs/MODULES.md](docs/MODULES.md) for what a module can do. `tools/module-index.py` rebuilds the catalogue the panel browses.

## Rules for this repository

- No secrets and nothing from anyone's own installation: no keys, addresses or host
  names. Site settings live in `/etc/mscos/site.env` on the device, secrets in
  `/etc/mscos/panel.env` and `/etc/soloist.env`.
- No player binaries and no keys are redistributed. Spotify's Soloist key is personal and
  Soloist may not be redistributed; Signalyst's NAA daemon and Roon Bridge are proprietary.
  Their install scripts download them from the publisher onto the user's device.
- The panel is made for the local network. Without an HTTPS reverse proxy in front of it,
  the password crosses the LAN in clear text; do not expose it to the internet.

## License

MIT, see [LICENSE](LICENSE). The kernel patch and module in `kernel/` are
GPL-2.0-or-later. The fonts (Jost, Cormorant) are under the SIL Open Font License.
Spotify, Roon, HQPlayer, Mark Levinson, Arcam and SOtM are trademarks of their owners;
mscOS is not affiliated with or endorsed by any of them.
