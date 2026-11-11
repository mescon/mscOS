#!/usr/bin/env bash
# Build an mscOS image: Armbian (Debian 13) with the mscOS kernel configuration, the native
# DSD patch and the mscOS packages.
#
#   image/build.sh sotm     SOtM sMS-200 / sMS-200ultra (Allwinner A20; Armbian board cubietruck)
#   image/build.sh rpi      Raspberry Pi 4, 5, CM4, CM5   (not tested on a device yet)
#   image/build.sh pc       x86-64 PCs with UEFI           (not tested on a device yet)
#   image/build.sh arm64    arm64 boards with UEFI         (not tested on a device yet)
#
# Needs Docker, about 30 GB of disk and, the first time, an hour or so for the kernel.
# The Armbian build framework is cloned to kernel/armbian-build (ARMBIAN_BUILD overrides).
# The image lands in <armbian-build>/output/images/. Extra arguments go to compile.sh.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd); ROOT=$(dirname "$HERE")
case ${1:-} in
    sotm) BOARD=cubietruck ;; rpi) BOARD=rpi4b ;; pc) BOARD=uefi-x86 ;; arm64) BOARD=uefi-arm64 ;;
    *) sed -n '2,13p' "$0"; exit 1 ;;
esac
shift
AB=${ARMBIAN_BUILD:-$ROOT/kernel/armbian-build}
[ -d "$AB" ] || git clone --depth 1 https://github.com/armbian/build "$AB"

"$ROOT/packaging/build.sh"
UP=$AB/userpatches
mkdir -p "$UP/extensions" "$UP/overlay/mscos/debs" "$UP/kernel/archive"
rm -f "$UP"/overlay/mscos/debs/*.deb "$UP/extensions/sotm-audio.sh"
cp "$ROOT"/packaging/out/*.deb "$UP/overlay/mscos/debs/"
cp "$HERE/userpatches/customize-image.sh" "$UP/customize-image.sh"
cp "$HERE/userpatches/extensions/mscos.sh" "$UP/extensions/mscos.sh"
cp "$ROOT"/kernel/userpatches/*.config "$UP/"
# The native DSD patch (sound/usb, the same for every board) into each family's patch directory.
for fam in sunxi uefi-x86 uefi-arm64 bcm2711; do
    mkdir -p "$UP/kernel/archive/$fam-6.18"
    cp "$ROOT"/kernel/userpatches/kernel/archive/sunxi-6.18/*.patch "$UP/kernel/archive/$fam-6.18/"
done

cd "$AB"
exec ./compile.sh build BOARD="$BOARD" BRANCH=current RELEASE=trixie BUILD_MINIMAL=yes BUILD_DESKTOP=no \
    KERNEL_CONFIGURE=no KERNEL_GIT=shallow KERNEL_BTF=no ENABLE_EXTENSIONS=mscos \
    HOST=mscos CONSOLE_AUTOLOGIN=no COMPRESS_OUTPUTIMAGE=sha,img,xz "$@"
