#!/usr/bin/env bash
# Spotify Lossless: install Soloist, Spotify's Connect endpoint for developers, on this device.
# The program is downloaded from Spotify (it may not be redistributed) and refreshed nightly,
# since each build expires after 90 days. It starts once a key has been entered in the panel
# (System -> Spotify): the key is personal and is never part of mscOS.
HERE=$(cd "$(dirname "$0")" && pwd)
. "${MSCOS_COMMON:-$HERE/../common.sh}"
DEVICE_NAME=${DEVICE_NAME:-mscOS}

echo "Downloading Soloist from Spotify…"
install -m 755 "$HERE/soloist-update" /usr/local/sbin/soloist-update
/usr/local/sbin/soloist-update --no-restart

# The key file: readable by the audio user only. Kept if it already has a key.
[ -e /etc/soloist.env ] || install -o root -g "$AUDIO_USER" -m 640 /dev/null /etc/soloist.env

if ! run_user test -e "$USER_UNITS/soloist.service"; then  # keep the device name of an existing install
    sed "s|@DEVICE_NAME@|$DEVICE_NAME|" "$HERE/soloist.service" | install_user_unit - soloist.service
fi
install -m 755 "$HERE/soloist-rt" /usr/local/bin/soloist-rt
install_user_unit "$HERE/soloist-rt.service"
install -m 644 "$HERE/soloist-update.service" "$HERE/soloist-update.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now soloist-update.timer
echo "Soloist installed: $(/usr/local/bin/soloist --version). Enter your key in the panel to start it."
