#!/usr/bin/env bash
# System-level tuning for the sMS-200Ultra audio endpoint. Run as root ON THE DEVICE
# after setup-soloist.sh (called from it). Idempotent. Takes effect fully after reboot.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
SVC_USER=soloist
UID_=$(id -u "$SVC_USER")
GID_=$(id -g "$SVC_USER")
H=$(getent passwd "$SVC_USER" | cut -d: -f6)
ENV=/boot/armbianEnv.txt

add_extraarg() {   # add_extraarg <arg> : append to extraargs= once
    if grep -q '^extraargs=' "$ENV"; then
        grep -q "^extraargs=.*\b$1\b" "$ENV" || sed -i "s|^extraargs=\(.*\)|extraargs=\1 $1|" "$ENV"
    else
        echo "extraargs=$1" >> "$ENV"
    fi
}

# --- 1. CPU split: audio on CPU1, everything else on CPU0 -----------------------
add_extraarg irqaffinity=0
add_extraarg audit=0   # AppArmor keeps CONFIG_AUDIT=y; no audit records on every syscall
# Console log level 0 (as ROCK): Armbian's boot.cmd passes loglevel=${verbosity}
sed -i 's/^verbosity=.*/verbosity=0/' "$ENV"

# Swedish time in logs and timestamps (the image ships with UTC)
timedatectl set-timezone Europe/Stockholm
install -d /etc/systemd/system.conf.d "/etc/systemd/system/user@$UID_.service.d"
printf '[Manager]\n# Default for all services: CPU0. The audio user gets CPU1 (user@%s drop-in).\nCPUAffinity=0\n' "$UID_" \
    > /etc/systemd/system.conf.d/10-sotm-cpu.conf
printf '[Service]\n# PipeWire, WirePlumber, Soloist (+ soloist-rt) on the audio core, next to the USB IRQs.\nCPUAffinity=1\n' \
    > "/etc/systemd/system/user@$UID_.service.d/10-audio-cpu.conf"
# Hardware watchdog (A20 sunxi-wdt, max 16 s): reboot automatically if the system hangs.
printf '[Manager]\nRuntimeWatchdogSec=15s\nRebootWatchdogSec=10min\n' > /etc/systemd/system.conf.d/20-watchdog.conf
install -m 755 "$HERE/sotm-tuning" /usr/local/sbin/sotm-tuning
sed "s/@982/@$UID_/g" "$HERE/sotm-tuning.service" > /etc/systemd/system/sotm-tuning.service
install -m 755 "$HERE/soloist-rt" /usr/local/bin/soloist-rt
install -o "$SVC_USER" -g "$SVC_USER" -m 644 "$HERE/soloist-rt.service" "$H/.config/systemd/user/soloist-rt.service"

# --- 2. Device tree: disable hardware the sMS-200Ultra does not have -------------
install -d /boot/overlay-user
dtc -q -@ -I dts -O dtb -o /boot/overlay-user/sotm-lean.dtbo "$HERE/sotm-lean.dts"
if grep -q '^user_overlays=' "$ENV"; then
    grep -q '^user_overlays=.*\bsotm-lean\b' "$ENV" || sed -i 's/^user_overlays=\(.*\)/user_overlays=\1 sotm-lean/' "$ENV"
else
    echo 'user_overlays=sotm-lean' >> "$ENV"
fi

# --- 3. Soloist's stream cache in RAM: no SD writes while playing ----------------
CACHE="$H/.cache"
grep -q " $CACHE tmpfs " /etc/fstab || \
    echo "tmpfs $CACHE tmpfs rw,nosuid,nodev,noatime,size=400M,mode=0755,uid=$UID_,gid=$GID_ 0 0" >> /etc/fstab

# --- 4. Nothing unscheduled during playback ---------------------------------------
# apt/man-db/e2scrub/dpkg backups run from the panel or not at all.
systemctl disable --now apt-daily.timer apt-daily-upgrade.timer man-db.timer \
    e2scrub_all.timer dpkg-db-backup.timer 2>/dev/null || true
sed -i 's|^@daily root /usr/lib/armbian/armbian-apt-updates|#&|' /etc/cron.d/armbian-updates 2>/dev/null || true
# Weekly trim and daily logrotate at 04:4x, next to the 04:30 Soloist update.
for t in fstrim:'Mon *-*-* 04:45:00' logrotate:'*-*-* 04:40:00'; do
    install -d "/etc/systemd/system/${t%%:*}.timer.d"
    printf '[Timer]\nOnCalendar=\nOnCalendar=%s\nRandomizedDelaySec=0\n' "${t#*:}" \
        > "/etc/systemd/system/${t%%:*}.timer.d/10-sotm.conf"
done
# Headless box: no HDMI console, journald already keeps the logs (rsyslog duplicated them).
systemctl disable --now rsyslog.service getty@tty1.service 2>/dev/null || true

systemctl daemon-reload
systemctl enable sotm-tuning.service
echo TUNE_OK "(reboot to apply overlay, irqaffinity, CPU split and cache tmpfs)"
