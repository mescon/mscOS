#!/usr/bin/env bash
# Prepare an Armbian (Cubietruck, Trixie) image for headless use on the SOtM sMS-200Ultra.
# Runs as root on the build machine. Modifies the IMAGE FILE only, never a block device.
#
# Usage: prepare-armbian-image.sh <image.img> <ssh-pubkey> [fdtfile]
set -euo pipefail

IMG=${1:?image file}
PUBKEY=${2:?ssh public key file}
FDT=${3:-}

[[ -f $IMG && ! -b $IMG ]] || { echo "refusing: $IMG is not a regular file" >&2; exit 1; }

MNT=$(mktemp -d)
LOOP=$(losetup --find --show --partscan "$IMG")
cleanup() { umount "$MNT" 2>/dev/null || true; losetup -d "$LOOP"; rmdir "$MNT"; }
trap cleanup EXIT

mount "${LOOP}p1" "$MNT"

# 1. Skip the interactive first-login wizard; access is via SSH key only.
rm -f "$MNT/root/.not_logged_in_yet"
install -d -m 700 "$MNT/root/.ssh"
install -m 600 "$PUBKEY" "$MNT/root/.ssh/authorized_keys"

# 2. Key-only SSH, root allowed only with key.
cat > "$MNT/etc/ssh/sshd_config.d/10-sotm.conf" <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
EOF

# 3. Lock root password (Armbian default is a well-known one).
sed -i -E 's|^root:[^:]*:|root:!*:|' "$MNT/etc/shadow"

# 4. Hostname.
echo sotm > "$MNT/etc/hostname"
sed -i 's/\bcubietruck\b/sotm/g' "$MNT/etc/hosts"

# 5. Optional: force a specific device tree.
if [[ -n $FDT ]]; then
    sed -i '/^fdtfile=/d' "$MNT/boot/armbianEnv.txt"
    echo "fdtfile=$FDT" >> "$MNT/boot/armbianEnv.txt"
fi

echo "--- armbianEnv.txt"; cat "$MNT/boot/armbianEnv.txt"
echo "--- authorized_keys"; ssh-keygen -lf "$MNT/root/.ssh/authorized_keys"
echo "--- root shadow"; grep '^root:' "$MNT/etc/shadow" | cut -d: -f1-2
echo PREPARE_OK
