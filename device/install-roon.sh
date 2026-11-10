#!/bin/bash
# Install Roon Bridge on the sMS-200Ultra (Armbian, armv7hf) as a switchable source.
# Run as root on the device, next to roonbridge.service. The panel's source switch
# (Spotify | Roon) starts/stops it; it is deliberately not enabled at boot.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
URL=https://download.roonlabs.net/builds/RoonBridge_linuxarmv7hf.tar.bz2

DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends libicu76 bzip2
if [ ! -x /opt/RoonBridge/start.sh ]; then
    tmp=$(mktemp -d)
    curl -fsSL -o "$tmp/rb.tar.bz2" "$URL"
    tar -xjf "$tmp/rb.tar.bz2" -C "$tmp"
    mv "$tmp/RoonBridge" /opt/RoonBridge
    rm -r "$tmp"
fi
/opt/RoonBridge/check.sh
install -m 644 "$HERE/roonbridge.service" /etc/systemd/system/roonbridge.service
# .NET starts slowly on the A20: without these RoonBridge gives up on RAATServer before it is
# listening (~6 s) and restarts it in a loop.
install -d /etc/systemd/system/roonbridge.service.d
install -m 644 "$HERE/roonbridge.service.d/10-dotnet-startup.conf" /etc/systemd/system/roonbridge.service.d/
# RAATServer onto the audio core once it is up (follows roonbridge: BindsTo/WantedBy)
install -m 755 "$HERE/raat-cpu" /usr/local/sbin/raat-cpu
install -m 644 "$HERE/raat-cpu.service" /etc/systemd/system/raat-cpu.service
systemctl daemon-reload
systemctl enable raat-cpu.service
systemctl disable roonbridge.service 2>/dev/null || true
echo "Roon Bridge installed. Switch source in the panel (Spelar → Källa)."
