#!/usr/bin/env bash
# HQPlayer NAA: install Signalyst's Network Audio Adapter daemon (networkaudiod) on this
# device. It is proprietary and may not be redistributed, so the package is downloaded from
# signalyst.eu here; using it requires a valid HQPlayer licence (its licence agreement is in
# /usr/share/doc/networkaudiod/copyright). Not enabled at boot: the panel's source switch
# starts and stops it, so that only one player at a time owns the DAC.
#   NAA_SERIES=5 installs the newest 5.x (for HQPlayer 4 and 5 hosts that need the older protocol).
HERE=$(cd "$(dirname "$0")" && pwd)
. "${MSCOS_COMMON:-$HERE/../common.sh}"
BASE=https://www.signalyst.eu/bins/naa/linux/trixie
SERIES=${NAA_SERIES:-}

tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
fetch "$BASE/" "$tmp/index.html"
DEB=$(grep -o "networkaudiod_${SERIES}[0-9.]*-[0-9]*_${ARCH}\.deb" "$tmp/index.html" | sort -uV | tail -n1)
[ -n "$DEB" ] || { echo "No networkaudiod package for $ARCH found at $BASE" >&2; exit 1; }
echo "Downloading $DEB from Signalyst…"
fetch "$BASE/$DEB" "$tmp/$DEB"
WAS=$(systemctl is-active networkaudiod.service 2>/dev/null || true)  # an update while it is the source
apt-get install -y -q --no-install-recommends "$tmp/$DEB"
systemctl disable networkaudiod.service 2>/dev/null || true
if [ "$WAS" = active ]; then systemctl restart networkaudiod.service
else systemctl stop networkaudiod.service 2>/dev/null || true; fi
echo "HQPlayer NAA installed ($DEB). Choose HQPlayer as the source in the panel."
