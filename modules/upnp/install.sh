#!/usr/bin/env bash
# UPnP renderer: MPD (from Debian) plays, upmpdcli (from its author's package repository at
# lesbonscomptes.com) is the UPnP and OpenHome face in front of it. Neither is enabled at
# boot: the panel's source switch starts and stops them, so that only one player at a time
# owns the DAC. Their settings are written by the panel when UPnP is chosen as the source.
HERE=$(cd "$(dirname "$0")" && pwd)
. "${MSCOS_COMMON:-$HERE/../common.sh}"
SITE=https://www.lesbonscomptes.com
# the author's "debian" repository has amd64 and armhf, the "raspbian" one arm64 (and armhf)
case $ARCH in arm64) REPO=raspbian ;; *) REPO=debian ;; esac
CODENAME=$(. /etc/os-release && echo "${VERSION_CODENAME:-}")
[ -n "$CODENAME" ] || { echo "Cannot tell which Debian release this is." >&2; exit 1; }

apt-get install -y -q --no-install-recommends curl ca-certificates
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
echo "Adding the upmpdcli package repository ($SITE)…"
fetch "$SITE/pages/lesbonscomptes.gpg" "$tmp/key.gpg"
install -m 644 "$tmp/key.gpg" /usr/share/keyrings/lesbonscomptes.gpg
echo "deb [signed-by=/usr/share/keyrings/lesbonscomptes.gpg] $SITE/upmpdcli/downloads/$REPO/ $CODENAME main" \
    > /etc/apt/sources.list.d/upmpdcli.list
apt-get update -q

WAS=$(systemctl is-active upmpdcli.service 2>/dev/null || true)  # an update while it is the source
apt-get install -y -q --no-install-recommends mpd upmpdcli
systemctl disable mpd.service mpd.socket upmpdcli.service 2>/dev/null || true
if [ "$WAS" = active ]; then systemctl restart mpd.service upmpdcli.service
else systemctl stop mpd.socket mpd.service upmpdcli.service 2>/dev/null || true; fi
echo "UPnP renderer installed (upmpdcli $(dpkg-query -W -f '${Version}' upmpdcli)). Choose UPnP as the source in the panel."
