#!/usr/bin/env bash
# Deploy the control panel to a device from a checkout (development; released installs get
# the mscos-panel package, which uses the same paths). Usage: web/deploy.sh [root@host]
set -euo pipefail
HOST=${1:-${MSCOS_HOST:-root@mscos.local}}
HERE=$(cd "$(dirname "$0")" && pwd)
P=/usr/lib/mscos/panel
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
mkdir -p '$P'/backend '$P'/static /var/lib/mscos /etc/mscos'
rsync -a --delete --exclude __pycache__ "$HERE/backend/" "$HOST:$P/backend/"
rsync -a --delete "$HERE/static/" "$HOST:$P/static/"
# the sources' install scripts (they download each player from its publisher, on the device)
rsync -a --delete "$HERE/../sources/" "$HOST:/usr/lib/mscos/sources/"
# mscOS release: date and commit of what is deployed (shown in the panel under System)
VER="$(date +%Y.%m.%d)-$(git -C "$HERE" describe --always --dirty 2>/dev/null || echo unknown)"
ssh "$HOST" "printf 'NAME=mscOS\nVERSION=%s\nHOME_URL=https://github.com/mescon/mscOS\n' '$VER' > /etc/mscos-release"
scp -q "$HERE/mscos-panel.service" "$HOST:/etc/systemd/system/mscos-panel.service"
ssh "$HOST" 'systemctl daemon-reload && systemctl enable mscos-panel.service >/dev/null 2>&1; systemctl restart mscos-panel.service; sleep 3; systemctl is-active mscos-panel.service'
