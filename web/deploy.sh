#!/usr/bin/env bash
# Development install: put this checkout on a device, at the same paths the packages use
# (packaging/build.sh --stage lays the files out). Usage: web/deploy.sh [root@host]
# Released installs use the packages instead (packaging/build.sh, or the image).
set -euo pipefail
HOST=${1:-${MSCOS_HOST:-root@mscos.local}}
HERE=$(cd "$(dirname "$0")" && pwd)
STAGE=$(mktemp -d); trap 'rm -rf "$STAGE"' EXIT
"$HERE/../packaging/build.sh" --stage "$STAGE" >/dev/null
# An install from before the panel was mscos-panel: move its state and secrets, drop the old unit.
ssh "$HOST" 'set -e
if [ -e /etc/systemd/system/sotm-web.service ]; then
    systemctl disable --now sotm-web.service >/dev/null 2>&1 || true
    [ -e /var/lib/mscos ] || mv /var/lib/sotm-web /var/lib/mscos
    install -d -m 755 /etc/mscos
    [ ! -e /etc/sotm-web.env ] || mv /etc/sotm-web.env /etc/mscos/panel.env
    [ ! -e /etc/mscos/site.env ] || sed -i "s/^SOTM_/MSCOS_/" /etc/mscos/site.env
    rm -rf /etc/systemd/system/sotm-web.service /opt/sotm-web
fi
rm -f /etc/systemd/system/mscos-panel.service   # now in /usr/lib/systemd/system
mkdir -p /usr/lib/mscos /usr/share/mscos /var/lib/mscos /etc/mscos'
# Music may be playing on the device: copy at the lowest CPU and disk priority, and reload
# systemd (seconds of work for PID 1 on a small board) only when a unit file changed.
LOW=(--rsync-path="nice -n19 ionice -c3 rsync")
UNITS=$(rsync -a "${LOW[@]}" --itemize-changes "$STAGE/usr/lib/systemd/system/" "$HOST:/usr/lib/systemd/system/")
rsync -a "${LOW[@]}" --delete "$STAGE/usr/lib/mscos/" "$HOST:/usr/lib/mscos/"
rsync -a "${LOW[@]}" --delete "$STAGE/usr/share/mscos/" "$HOST:/usr/share/mscos/"
rsync -a "${LOW[@]}" "$STAGE/etc/mscos-release" "$HOST:/etc/mscos-release"
if [ -n "$UNITS" ]; then ssh "$HOST" 'systemctl daemon-reload && systemctl enable mscos-panel.service >/dev/null 2>&1' || true; fi
ssh "$HOST" 'systemctl restart mscos-panel.service; sleep 3; systemctl is-active mscos-panel.service'
