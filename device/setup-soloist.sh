#!/usr/bin/env bash
# Configure Armbian on SOtM sMS-200Ultra as a bit-perfect Spotify Soloist endpoint.
# Run as root ON THE DEVICE. Expects /root/soloist.env (SOLOIST_API_KEY=...) to exist.
# Idempotent: safe to re-run.
set -euo pipefail

SVC_USER=soloist
DEVICE_NAME=${DEVICE_NAME:-"sMS-200Ultra"}
HERE=$(cd "$(dirname "$0")" && pwd)

# --- 1. CPU: fixed 960 MHz, performance governor (as Eunhasu) ----------------
cat > /etc/default/cpufrequtils <<'EOF'
ENABLE=true
GOVERNOR=performance
MIN_SPEED=960000
MAX_SPEED=960000
EOF

# --- 2. Threaded IRQs so rtirq can prioritise USB audio ----------------------
# --- 2. Allow SCHED_FIFO inside systemd cgroups ------------------------------
# Armbian's kernel has CONFIG_RT_GROUP_SCHED=y, which gives every non-root cgroup a
# zero RT budget, so PipeWire's data-loop silently stays SCHED_OTHER. Turn it off.
# (threadirqs is NOT used: on this ARM32 kernel it WARNs in early boot and is ignored,
#  so rtirq has nothing to prioritise. Revisit with a custom PREEMPT kernel.)
if grep -q '^extraargs=' /boot/armbianEnv.txt; then
    grep -q rt_group_sched=0 /boot/armbianEnv.txt || sed -i 's/^extraargs=/extraargs=rt_group_sched=0 /' /boot/armbianEnv.txt
else
    echo 'extraargs=rt_group_sched=0' >> /boot/armbianEnv.txt
fi
systemctl disable rtirq.service 2>/dev/null || true

# --- 3. Trim services that do nothing on a wired audio endpoint --------------
systemctl disable --now wpa_supplicant.service 2>/dev/null || true

# --- 4. Service user with realtime limits (Debian: @pipewire rtprio 95) -------
id -u "$SVC_USER" >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/soloist \
    --shell /usr/sbin/nologin --groups audio,pipewire "$SVC_USER"
loginctl enable-linger "$SVC_USER"
H=$(getent passwd "$SVC_USER" | cut -d: -f6)

install -o root -g "$SVC_USER" -m 640 /root/soloist.env /etc/soloist.env

# --- 5. PipeWire: 44.1 kHz only, no resampling, big quantum, never suspend ----
install -d -o "$SVC_USER" -g "$SVC_USER" "$H/.config" \
    "$H/.config/pipewire/pipewire.conf.d" "$H/.config/wireplumber/wireplumber.conf.d" \
    "$H/.config/systemd/user"
install -o "$SVC_USER" -g "$SVC_USER" -m 644 "$HERE/10-bitperfect.conf" \
    "$H/.config/pipewire/pipewire.conf.d/10-bitperfect.conf"
install -o "$SVC_USER" -g "$SVC_USER" -m 644 "$HERE/50-usb-dac.conf" \
    "$H/.config/wireplumber/wireplumber.conf.d/50-usb-dac.conf"

# --- 6. Soloist binary + updater ---------------------------------------------
install -m 755 "$HERE/soloist-update" /usr/local/sbin/soloist-update
/usr/local/sbin/soloist-update --no-restart

install -o "$SVC_USER" -g "$SVC_USER" -m 644 "$HERE/soloist.service" "$H/.config/systemd/user/soloist.service"
sed -i "s|@DEVICE_NAME@|$DEVICE_NAME|" "$H/.config/systemd/user/soloist.service"
install -m 644 "$HERE/soloist-update.service" /etc/systemd/system/soloist-update.service
install -m 644 "$HERE/soloist-update.timer"   /etc/systemd/system/soloist-update.timer
systemctl daemon-reload
systemctl enable soloist-update.timer

# Enable user units for the linger session.
UID_=$(id -u "$SVC_USER")
systemctl start "user@$UID_.service"
run_user() { runuser -u "$SVC_USER" -- env XDG_RUNTIME_DIR=/run/user/$UID_ DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/$UID_/bus "$@"; }
run_user systemctl --user daemon-reload
run_user systemctl --user enable pipewire.socket wireplumber.service soloist.service

# --- 7. System tuning: CPU/IRQ split, DT overlay, RAM cache, quiet timers -----
"$HERE/tune-system.sh"
run_user systemctl --user daemon-reload
run_user systemctl --user enable soloist-rt.service
echo SETUP_OK
