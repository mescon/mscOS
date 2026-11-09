#!/usr/bin/env bash
# Deploy the control panel to the device. Usage: web/deploy.sh [root@host]
set -euo pipefail
HOST=${1:-root@192.168.1.200}
HERE=$(cd "$(dirname "$0")" && pwd)
ssh "$HOST" 'mkdir -p /opt/sotm-web/backend /opt/sotm-web/static /var/lib/sotm-web'
rsync -a --delete --exclude __pycache__ "$HERE/backend/" "$HOST:/opt/sotm-web/backend/"
rsync -a --delete "$HERE/static/" "$HOST:/opt/sotm-web/static/"
scp -q "$HERE/sotm-web.service" "$HOST:/etc/systemd/system/sotm-web.service"
ssh "$HOST" 'systemctl daemon-reload && systemctl enable sotm-web.service >/dev/null 2>&1; systemctl restart sotm-web.service; sleep 3; systemctl is-active sotm-web.service'
