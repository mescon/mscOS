#!/usr/bin/env bash
# Build the mscOS packages, or lay out their files for a development install.
#
#   packaging/build.sh              -> packaging/out/*.deb   (needs dpkg-deb; else runs in Docker)
#   packaging/build.sh --stage DIR  -> the files of mscos-core and mscos-panel under DIR, at
#                                      their installed paths (deploy.sh copies that to a device)
#
# Packages (all architecture-independent; players are downloaded on the device):
#   mscos-core       audio user, PipeWire configuration, board profiles, boot-time tuning
#   mscos-panel      the control panel and the modules (with the sources' install scripts)
#   mscos-dsd-dkms   native DSD for C-Media CM6632A on kernels without the mscOS patch
#   mscos            depends on core and panel
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(dirname "$HERE")
VERSION=${MSCOS_VERSION:-$(date +%Y.%m.%d)+$(git -C "$ROOT" rev-list --count HEAD 2>/dev/null || echo 0)}
COMMIT=${MSCOS_COMMIT:-$(git -C "$ROOT" describe --always --dirty 2>/dev/null || echo unknown)}

stage_core() {  # <dir>
    install -d "$1/usr/lib/mscos/bin" "$1/usr/share/mscos/boards/dts" "$1/usr/share/mscos/pipewire" \
        "$1/usr/lib/systemd/system" "$1/etc"
    install -m 755 "$ROOT"/core/mscos-setup "$ROOT"/core/mscos-board "$ROOT"/core/mscos-tuning "$ROOT"/core/mscos-update \
        "$ROOT"/core/mscos-verify "$1/usr/lib/mscos/bin/"
    install -d "$1/usr/share/mscos/keys"   # the project's public keys: releases and the module catalogue are signed
    install -m 644 "$ROOT"/core/keys/*.pem "$1/usr/share/mscos/keys/"
    install -m 644 "$ROOT"/boards/*.conf "$1/usr/share/mscos/boards/"
    install -m 644 "$ROOT"/boards/dts/*.dts "$1/usr/share/mscos/boards/dts/"
    install -m 644 "$ROOT"/core/10-bitperfect.conf "$ROOT"/core/50-usb-dac.conf "$1/usr/share/mscos/pipewire/"
    install -m 644 "$ROOT"/core/mscos-tuning.service "$ROOT"/core/mscos-firstboot.service "$1/usr/lib/systemd/system/"
    printf 'NAME=mscOS\nVERSION=%s\nBUILD=%s\nHOME_URL=https://github.com/mescon/mscOS\n' "$VERSION" "$COMMIT" > "$1/etc/mscos-release"
}
stage_panel() {  # <dir>
    install -d "$1/usr/lib/mscos/panel" "$1/usr/lib/mscos/modules" "$1/usr/lib/systemd/system"
    cp -r "$ROOT/web/backend" "$ROOT/web/static" "$1/usr/lib/mscos/panel/"
    find "$1/usr/lib/mscos/panel" -name __pycache__ -prune -exec rm -rf {} +
    # the page cache is named after the version, so every build replaces the one before it
    sed -i "s/^const CACHE = \"mscos-v[0-9]*\";/const CACHE = \"mscos-$VERSION\";/" "$1/usr/lib/mscos/panel/static/sw.js"
    cp -r "$ROOT"/modules/. "$1/usr/lib/mscos/modules/"
    find "$1/usr/lib/mscos/modules" -name __pycache__ -prune -exec rm -rf {} +
    rm -f "$1/usr/lib/mscos/modules/index.json" "$1/usr/lib/mscos/modules/index.json.sig" "$1/usr/lib/mscos/modules/README.md"
    install -m 644 "$ROOT/web/mscos-panel.service" "$1/usr/lib/systemd/system/"
}
stage_dkms() {  # <dir>
    d="$1/usr/src/mscos-dsd-$VERSION"
    install -d "$d"
    sed "s/@VERSION@/$VERSION/" "$ROOT/kernel/mscos-dsd/dkms/dkms.conf" > "$d/dkms.conf"
    install -m 755 "$ROOT/kernel/mscos-dsd/dkms/prepare.sh" "$d/prepare.sh"
    install -m 644 "$ROOT"/kernel/mscos-dsd/0001-*.patch "$d/mscos-dsd.patch"
}

if [ "${1:-}" = --stage ]; then
    rm -rf "${2:?directory}"; mkdir -p "$2"
    stage_core "$2"; stage_panel "$2"
    echo "$VERSION"
    exit 0
fi

if ! command -v dpkg-deb >/dev/null; then  # not a Debian machine: build in a container
    exec docker run --rm -v "$ROOT:/src" -w /src -e MSCOS_VERSION="$VERSION" -e MSCOS_COMMIT="$COMMIT" -u "$(id -u):$(id -g)" \
        -e HOME=/tmp debian:trixie-slim packaging/build.sh
fi

OUT=$HERE/out; WORK=$(mktemp -d); trap 'rm -rf "$WORK"' EXIT
rm -rf "$OUT"; mkdir -p "$OUT"
pkg() {  # <name> : control + maintainer scripts from packaging/<name>/, files from $WORK/<name>
    local n=$1 d=$WORK/$1
    install -d "$d/DEBIAN"
    sed "s/@VERSION@/$VERSION/g" "$HERE/$n/control" > "$d/DEBIAN/control"
    for s in preinst postinst prerm postrm; do
        [ -f "$HERE/$n/$s" ] && sed "s/@VERSION@/$VERSION/g" "$HERE/$n/$s" > "$d/DEBIAN/$s" && chmod 755 "$d/DEBIAN/$s"
    done
    dpkg-deb --root-owner-group --build "$d" "$OUT/${n}_${VERSION}_all.deb" >/dev/null
    echo "built ${n}_${VERSION}_all.deb"
}
mkdir -p "$WORK"/{mscos-core,mscos-panel,mscos-dsd-dkms,mscos}
stage_core "$WORK/mscos-core";   pkg mscos-core
stage_panel "$WORK/mscos-panel"; pkg mscos-panel
stage_dkms "$WORK/mscos-dsd-dkms"; pkg mscos-dsd-dkms
pkg mscos
