#!/usr/bin/env bash
# Build snd-usb-audio.ko with the mscOS native-DSD patch, against the kernel tree the
# running image was built from (same .config, no module signing, no MODVERSIONS, so a
# matching vermagic is all a module needs).
#
#   kernel/mscos-dsd/build-module.sh [kernel-tree]
#
# The tree defaults to the Armbian worktree of the last kernel build. The build runs in
# Armbian's build container (the same arm-linux-gnueabihf-gcc 14.2 the kernel was built
# with); the tree is mounted read-only. Output: kernel/mscos-dsd/out/<release>/snd-usb-audio.ko
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
TREE=${1:-$HERE/../armbian-build/cache/sources/linux-kernel-worktree/6.18__sunxi__armhf}
TREE=$(cd "$TREE" && pwd)
IMAGE=${IMAGE:-ghcr.io/armbian/docker-armbian-build:armbian-debian-trixie-latest}
REL=$(cat "$TREE/include/config/kernel.release")
WORK=$(mktemp -d)
trap 'rm -rf "$WORK"' EXIT

cp -r "$TREE/sound/usb" "$WORK/usb"
find "$WORK/usb" \( -name '*.o' -o -name '*.ko' -o -name '*.mod*' -o -name '.*.cmd' \) -delete
(cd "$WORK" && mkdir -p a/sound && mv usb a/sound/usb && cd a && patch -p1 -s < "$HERE/0001-ALSA-usb-audio-mscOS-native-DSD-for-CM6632A.patch")
docker run --rm -v "$TREE:/kernel:ro" -v "$WORK/a/sound/usb:/build" -u "$(id -u):$(id -g)" "$IMAGE" \
    make -C /kernel M=/build ARCH=arm CROSS_COMPILE=arm-linux-gnueabihf- -j"$(nproc)" modules >/dev/null
mkdir -p "$HERE/out/$REL"
cp "$WORK/a/sound/usb/snd-usb-audio.ko" "$HERE/out/$REL/"
modinfo "$HERE/out/$REL/snd-usb-audio.ko" | grep -E '^vermagic|mscos'
echo "built $HERE/out/$REL/snd-usb-audio.ko"
