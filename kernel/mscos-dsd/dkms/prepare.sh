#!/bin/sh
# DKMS PRE_BUILD: put the patched sound/usb for kernel $1 in ./usb.
#
# The kernel release (6.18.62-current-sunxi) names the upstream version (v6.18.62). Only
# sound/usb of that tag is fetched from the stable tree (a sparse, blobless clone: a few MB).
# Distribution kernels rarely touch sound/usb; if the patch does not apply, the build stops
# here and the stock driver stays in use.
set -eu
KVER=$1
V=$(echo "$KVER" | sed -n 's/^\([0-9]*\.[0-9]*\)\(\.[0-9]*\)\{0,1\}.*/\1\2/p')
[ -n "$V" ] || { echo "mscos-dsd: cannot tell the upstream version of $KVER" >&2; exit 1; }
V=${V%.0}
REPO=${MSCOS_KERNEL_GIT:-https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git}
rm -rf usb linux-sound-usb
git -c advice.detachedHead=false clone --quiet --depth 1 --filter=blob:none --sparse --branch "v$V" "$REPO" linux-sound-usb
git -C linux-sound-usb sparse-checkout set --no-cone /sound/usb
mkdir -p a/sound && cp -r linux-sound-usb/sound/usb a/sound/usb
# -l: the same code is indented one step differently in older releases (6.12)
(cd a && patch -p1 -s -l < ../mscos-dsd.patch)
mv a/sound/usb usb && rm -rf a linux-sound-usb
echo "mscos-dsd: sound/usb of v$V patched for $KVER"
