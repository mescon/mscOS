#!/bin/bash
# mscOS image: runs inside the image (chroot) at the end of the Armbian build.
# arguments: $RELEASE $LINUXFAMILY $BOARD $BUILD_DESKTOP $ARCH
# The mscOS packages are in /tmp/overlay/mscos/debs (image/build.sh puts them there).
set -euo pipefail
BOARD=$3
export DEBIAN_FRONTEND=noninteractive

# The board profile, where the board is known at build time. The build machine is not the
# board, so its core count is given too. Elsewhere (PCs, unknown boards) mscos-setup finishes
# on the device at first boot (mscos-firstboot.service) and restarts once.
case $BOARD in
    cubietruck) export MSCOS_BOARD=sotm-sms200ultra MSCOS_NCPU=2 ;;
    rpi4b)      export MSCOS_BOARD=raspberrypi MSCOS_NCPU=4 ;;
esac

apt-get update -q
apt-get install -y -q --no-install-recommends avahi-daemon ethtool device-tree-compiler \
    /tmp/overlay/mscos/debs/mscos-core_*.deb /tmp/overlay/mscos/debs/mscos-panel_*.deb /tmp/overlay/mscos/debs/mscos_*.deb
apt-get clean

# An appliance: no first-login wizard on the console. Setup happens in the panel
# (http://mscos.local). Nobody can log in until the owner adds an SSH key: the root password
# is locked and SSH takes keys only (put one in /root/.ssh/authorized_keys on the card).
rm -f /root/.not_logged_in_yet
passwd -l root >/dev/null
install -d /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/10-mscos.conf <<'EOT'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
EOT
# Armbian's periodic apt check runs unscheduled; updates are started from the panel.
sed -i 's|^@daily root /usr/lib/armbian/armbian-apt-updates|#&|' /etc/cron.d/armbian-updates 2>/dev/null || true
echo "mscOS image: $(grep VERSION /etc/mscos-release) for $BOARD (profile ${MSCOS_BOARD:-chosen at first boot})"
