#!/usr/bin/env bash
# Install the patched snd-usb-audio.ko on the device, as an override in
# /lib/modules/<release>/updates/ (the stock module stays untouched).
#
#   kernel/mscos-dsd/install-module.sh [root@host]          install, active at next load
#   kernel/mscos-dsd/install-module.sh [root@host] --remove  back to the stock module
#
# The module is loaded at boot; to switch without a reboot, put the panel in Roon mode
# (nothing then holds the DAC) and run: rmmod snd_usb_audio && modprobe snd_usb_audio
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
HOST=${1:-root@192.168.1.200}
REL=$(ssh "$HOST" uname -r)
if [ "${2:-}" = "--remove" ]; then
    ssh "$HOST" "rm -f /lib/modules/$REL/updates/snd-usb-audio.ko && depmod -a && modinfo -n snd-usb-audio"
    exit 0
fi
KO="$HERE/out/$REL/snd-usb-audio.ko"
[ -f "$KO" ] || { echo "no build for $REL; run build-module.sh first" >&2; exit 1; }
want=$(modinfo -F vermagic "$KO")
have=$(ssh "$HOST" "modinfo -F vermagic snd-usb-audio")
[ "$want" = "$have" ] || { echo "vermagic differs: module '$want', device '$have'" >&2; exit 1; }
scp -q "$KO" "$HOST:/tmp/snd-usb-audio.ko"
ssh "$HOST" "install -D -m644 /tmp/snd-usb-audio.ko /lib/modules/$REL/updates/snd-usb-audio.ko && rm /tmp/snd-usb-audio.ko && depmod -a && modinfo -n snd-usb-audio"
