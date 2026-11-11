#!/usr/bin/env bash
# Spotify Connect without a key: install go-librespot (github.com/devgianlu/go-librespot),
# an independent open-source Spotify Connect client, on this device. Ogg Vorbis, 320 kbit/s
# at most. Downloads the latest release from GitHub; the panel writes its configuration.
HERE=$(cd "$(dirname "$0")" && pwd)
. "${MSCOS_COMMON:-$HERE/../common.sh}"
REPO=devgianlu/go-librespot
# The "armv6" build is ARMv7 hard-float code in practice (GOARM=6, VFP): right for armhf.
case $ARCH in armhf) A=armv6 ;; arm64) A=arm64 ;; amd64) A=x86_64 ;; esac

apt-get install -y -q --no-install-recommends libasound2t64 pipewire-alsa curl ca-certificates
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
# the tag the "latest release" link leads to; go-librespot cannot tell its own version
TAG=$(curl --fail --silent --show-error --location --head -o /dev/null -w '%{url_effective}' \
    "https://github.com/$REPO/releases/latest" | sed -n 's|.*/releases/tag/||p')
[ -n "$TAG" ] || { echo "Could not find the latest release of $REPO" >&2; exit 1; }
echo "Downloading go-librespot $TAG from GitHub…"
fetch "https://github.com/$REPO/releases/download/$TAG/go-librespot_linux_$A.tar.gz" "$tmp/g.tar.gz"
tar -xzf "$tmp/g.tar.gz" -C "$tmp" go-librespot
test -x "$tmp/go-librespot"
install -m 755 "$tmp/go-librespot" /usr/local/bin/go-librespot
install_user_unit "$HERE/librespot.service"
note_version librespot "$TAG"
run_user systemctl --user try-restart librespot.service || true  # an update while it plays
echo "go-librespot $TAG installed. Choose it as the source in the panel."
