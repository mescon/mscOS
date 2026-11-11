#!/usr/bin/env bash
# Roon Bridge: this device as a Roon endpoint. Downloaded from Roon Labs. It is not enabled
# at boot: the panel's source switch starts and stops it, so that only one player at a time
# owns the DAC (Roon opens it directly, which DSD needs).
HERE=$(cd "$(dirname "$0")" && pwd)
. "$HERE/../common.sh"
case $ARCH in armhf) A=armv7hf ;; arm64) A=armv8 ;; amd64) A=x64 ;; esac

apt-get install -y -q --no-install-recommends libasound2t64 libicu76 bzip2 curl ca-certificates
if [ ! -x /opt/RoonBridge/start.sh ]; then
    tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
    echo "Downloading Roon Bridge from Roon Labs…"
    fetch "https://download.roonlabs.net/builds/RoonBridge_linux$A.tar.bz2" "$tmp/rb.tar.bz2"
    tar -xjf "$tmp/rb.tar.bz2" -C "$tmp"
    mv "$tmp/RoonBridge" /opt/RoonBridge
fi
/opt/RoonBridge/check.sh
install -m 644 "$HERE/roonbridge.service" /etc/systemd/system/roonbridge.service
# With an audio core set by the board profile: .NET starts too slowly on one small core
# (RoonBridge gives up on RAATServer and restarts it in a loop), so it starts on all cores
# and raat-cpu moves RAATServer to the audio core once it is up.
. /etc/mscos/board.conf 2>/dev/null || true
if [ -n "${AUDIO_CPU:-}" ]; then
    install -d /etc/systemd/system/roonbridge.service.d
    install -m 644 "$HERE/roonbridge.service.d/10-dotnet-startup.conf" /etc/systemd/system/roonbridge.service.d/
    printf '[Service]\nCPUAffinity=\nCPUAffinity=0-%s\n' "$(($(nproc --all) - 1))" > /etc/systemd/system/roonbridge.service.d/20-all-cpus.conf
    install -m 755 "$HERE/raat-cpu" /usr/local/sbin/raat-cpu
    install -m 644 "$HERE/raat-cpu.service" /etc/systemd/system/raat-cpu.service
    systemctl daemon-reload
    systemctl enable raat-cpu.service
fi
systemctl daemon-reload
systemctl disable roonbridge.service 2>/dev/null || true
echo "Roon Bridge installed. Choose Roon as the source in the panel."
