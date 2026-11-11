# Shared by the sources' install scripts (sourced). The audio user owns the PipeWire session
# and the players that run in it: "mscos", or "soloist" on installs from before sources were
# modules.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
if [ -n "${MSCOS_AUDIO_USER:-}" ]; then AUDIO_USER=$MSCOS_AUDIO_USER
elif id -u mscos >/dev/null 2>&1; then AUDIO_USER=mscos
elif id -u soloist >/dev/null 2>&1; then AUDIO_USER=soloist
else echo "No audio user (mscos). Install mscos-core first." >&2; exit 1; fi
AUDIO_UID=$(id -u "$AUDIO_USER")
AUDIO_HOME=$(getent passwd "$AUDIO_USER" | cut -d: -f6)
USER_UNITS=$AUDIO_HOME/.config/systemd/user

case $(dpkg --print-architecture) in
    armhf) ARCH=armhf ;; arm64) ARCH=arm64 ;; amd64) ARCH=amd64 ;;
    *) echo "Unsupported architecture: $(dpkg --print-architecture)" >&2; exit 1 ;;
esac

run_user() {  # a command in the audio user's session
    runuser -u "$AUDIO_USER" -- env XDG_RUNTIME_DIR="/run/user/$AUDIO_UID" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$AUDIO_UID/bus" "$@"
}
install_user_unit() {  # install_user_unit <file> [name]
    install -d -o "$AUDIO_USER" -g "$AUDIO_USER" "$USER_UNITS"
    install -o "$AUDIO_USER" -g "$AUDIO_USER" -m 644 "$1" "$USER_UNITS/${2:-$(basename "$1")}"
    run_user systemctl --user daemon-reload
}
fetch() {  # fetch <url> <file>
    curl --fail --location --silent --show-error --retry 3 -o "$2" "$1"
}
