# Installing mscOS

Two ways: a ready image for a supported board, or the packages on a Debian 13 system you
already run. Either way the rest happens in the panel.

What you need:

- a board (see the table), an SD card of 8 GB or more, wired network,
- a USB DAC, or an amplifier with a USB input,
- for Spotify: a Premium account. For Spotify Lossless also a developer key of your own
  (free, created on Spotify's developer site). For Roon: a Roon server on your network.
  For HQPlayer NAA: HQPlayer on another computer.

| Board | Image target | Tested |
|---|---|---|
| SOtM sMS-200, sMS-200ultra | `sotm` | mscOS runs on an sMS-200ultra; the image builds but has not been started on a device yet |
| Raspberry Pi 4, 5, CM4, CM5 | `rpi` | the image builds and its contents are checked; not started on a device yet |
| x86-64 PC with UEFI (mini PC, NUC) | `pc` | starts in a virtual machine; not yet on hardware |
| arm64 board with UEFI | `arm64` | the image builds and its contents are checked; not started on a device yet |

## A. From an image

1. Build the image for your board: `image/build.sh <target>` (needs Docker; the image lands
   in `kernel/armbian-build/output/images/`). Building an ARM image on an x86 machine also
   needs qemu user emulation registered on the build machine (`qemu-user-static` with
   binfmt support).
2. Write it to the SD card with [balenaEtcher](https://etcher.balena.io/), Raspberry Pi
   Imager ("Use custom"), or `dd`. On a PC, write it to a USB stick or the internal disk.
3. Put the card in the device, connect network and DAC, and power it on. The first start
   takes a few minutes: the card is expanded, and on PCs the device restarts once by itself.
4. Open **`http://mscos.local`** in a browser on the same network. If the name does not
   resolve, use the device's IP address (your router lists it as `mscos`).
5. Follow the guide: language, a password for the panel, names, sources, amplifier, alerts.
6. The panel then opens **Profile, Getting started**, which lists what is left and takes
   each row away when it is done:
   - **Fetch the players** for the sources you chose: under **Modules**, press **Install**.
     Each player is downloaded from its publisher (Spotify, Roon Labs, Signalyst, GitHub).
   - **Enter your keys** under **Profile, Accounts and keys**. Under each field,
     "How to get it, step by step" walks you through the other site. Spotify Lossless
     needs your own developer key. Searching Spotify and seeing your library needs an app
     of your own at Spotify (its Client ID) and **Sign in with Spotify**.
   - **Put mscOS on your phone's home screen**, so it opens like an app. On an iPhone:
     Safari, Share, Add to Home Screen. On Android: the browser's menu, Add to Home screen
     or Install app.
7. Choose the source at the top of the panel and play: pick the device in the Spotify app,
   in Roon (Settings, Audio) or in HQPlayer.

SOtM sMS-200 and sMS-200ultra: the image replaces SOtM's Eunhasu on the SD card. Keep the
original card untouched and use another one; putting the original back restores the unit
exactly as it was.

### Logging in over SSH

The image has no login password. To get a shell, put your SSH public key in
`/root/.ssh/authorized_keys` on the card's root partition before the first start, then
`ssh root@mscos.local`.

## B. Packages on Debian 13 or Armbian

Build the packages (`packaging/build.sh`, on any machine with Docker or dpkg) and copy
`packaging/out/*.deb` to the device. Then, as root:

```sh
apt install ./mscos-core_*.deb ./mscos-panel_*.deb ./mscos_*.deb
reboot
```

`mscos-core` creates the audio user `mscos`, configures PipeWire for it, picks the board
profile and sets the kernel arguments and the CPU split, which take effect after the
restart. Then open `http://<hostname>.local` (with `avahi-daemon` installed) or the IP
address, and continue from step 5 above.

The stock Debian kernel works. The mscOS images use a kernel built with full preemption
and a 1000 Hz tick; the panel's optimizations list shows what your kernel has.

## Native DSD (Mark Levinson Nº585, Nº585.5 and other CM6632A DACs)

These DACs take raw DSD in a format Linux does not support. The mscOS images include the
driver patch. On another kernel, install `mscos-dsd-dkms` together with your kernel's
headers; it builds the patched `snd-usb-audio` for the running kernel. Turn on the module
**Native DSD for CM6632A** to see whether the DAC offers DSD. Roon and HQPlayer then send
DSD64 and DSD128 natively.

## Behind a reverse proxy (HTTPS)

By default the panel answers on plain HTTP to its own name and addresses. To put it behind
an HTTPS reverse proxy, create `/etc/mscos/site.env`:

```sh
MSCOS_PUBLIC_URL=https://music.example.org
MSCOS_TRUSTED_PEERS=192.0.2.10        # the proxy's address
```

and restart `mscos-panel`. The panel then only accepts the proxy, and sends everyone else
to the HTTPS name. Do not expose the panel to the internet.

## Updating

- The system: **System, Check for system updates** in the panel (apt).
- Soloist updates itself nightly (its builds expire after 90 days).
- The other players: **Modules, Check for updates** asks each publisher for its newest
  version and offers **Update** where there is one. Roon Bridge updates itself.
- mscOS itself: **System, Look for a new version of mscOS** installs newer packages from
  the project's releases, or install them the same way as in B.

### Signatures

A device installs a release, or a module from the catalogue, only when it carries the
project's signature. The public keys come with mscOS (`/usr/share/mscos/keys/`); the private
key is the maintainer's and is kept neither in the repository nor with the build service, so
a release or a catalogue published by someone who has taken the repository over is refused.
A release shows as "published but not signed yet" until the maintainer has signed it.
An older edition is not accepted again either: the catalogue carries a serial number inside
what is signed, and a release's signed list names each package with its version.

To check packages you downloaded by hand, get `SHA256SUMS` and `SHA256SUMS.sig` from the same
release and a public key from `core/keys/` in the repository:

    base64 -d SHA256SUMS.sig > sig.bin
    openssl pkeyutl -verify -pubin -inkey mscos-1.pem -rawin -in SHA256SUMS -sigfile sig.bin
    sha256sum --check SHA256SUMS

Running a fork or a mirror of your own: put your public key in `/etc/mscos/keys/` on the
device, or set `MSCOS_ALLOW_UNSIGNED=1` in `/etc/mscos/site.env` to do without signatures.
A module installed from another publisher's address (Modules, Install from an address) is
not covered by the project's signature: you are trusting that publisher.

## If something does not work

- **The panel does not open:** check that the device has an address on your network; try
  the IP address instead of `mscos.local`.
- **No DAC found:** the panel shows the USB negotiation under Signal, USB. Try another
  cable or port. A DAC that needs USB 2.0 for high sample rates must connect at high speed.
- **A source will not start:** System, Logs shows the player's own log.
- **Forgotten password:** with a shell on the device,
  `python3 /usr/lib/mscos/panel/backend/auth.py reset && systemctl restart mscos-panel`.
