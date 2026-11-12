#!/usr/bin/env bash
# Sign what devices install: the module catalogue and the packages of a release.
# For the maintainer. The private key stays on the maintainer's machine, never in the
# repository and never with the build service: a signature then says "the maintainer has
# looked at this", which a taken-over repository cannot say.
#
#   tools/sign.sh keygen            make a key pair: the private key in $MSCOS_SIGNING_KEY
#                                   (default ~/.config/mscos/signing-key.pem), the public key
#                                   in core/keys/, from where it is shipped with mscOS
#   tools/sign.sh catalogue         write modules/index.json.sig (after tools/module-index.py,
#                                   when a module has changed and you have reviewed it)
#   tools/sign.sh release <tag>     download the release's packages, list them, write
#                                   SHA256SUMS and SHA256SUMS.sig and attach both to the release
#   tools/sign.sh verify <file> <signature>     check a signature against core/keys/
#
# Devices verify with core/mscos-verify. A device accepts any key in /usr/share/mscos/keys,
# so a new key reaches them with a release signed by the old one; remove the old key's file
# from core/keys/ a release later.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
KEY=${MSCOS_SIGNING_KEY:-$HOME/.config/mscos/signing-key.pem}
KEYS=$ROOT/core/keys

sign() {  # <file> -> <file>.sig
    [ -f "$KEY" ] || { echo "No signing key at $KEY (tools/sign.sh keygen, or set MSCOS_SIGNING_KEY)" >&2; exit 1; }
    openssl pkeyutl -sign -inkey "$KEY" -rawin -in "$1" | base64 -w0 > "$1.sig"
    echo >> "$1.sig"
    MSCOS_KEYS=$KEYS "$ROOT/core/mscos-verify" "$1" "$1.sig" \
        || { rm -f "$1.sig"; echo "The key at $KEY has no public key in core/keys/: devices would refuse this." >&2; exit 1; }
}

case "${1:-}" in
keygen)
    [ ! -e "$KEY" ] || { echo "$KEY exists already; move it away first if you really want a new one." >&2; exit 1; }
    mkdir -p "$(dirname "$KEY")" "$KEYS"; chmod 700 "$(dirname "$KEY")"
    (umask 077; openssl genpkey -algorithm ed25519 -out "$KEY")
    n=1; while [ -e "$KEYS/mscos-$n.pem" ]; do n=$((n + 1)); done
    openssl pkey -in "$KEY" -pubout -out "$KEYS/mscos-$n.pem"
    echo "Private key: $KEY (keep a copy somewhere safe; it is not protected by a passphrase:"
    echo "             openssl pkey -in $KEY -aes256 -out $KEY.enc gives one that is)"
    echo "Public key:  core/keys/mscos-$n.pem (commit it)"
    ;;
catalogue)
    python3 "$ROOT/tools/module-index.py" --check
    sign "$ROOT/modules/index.json"
    echo "wrote modules/index.json.sig"
    ;;
release)
    tag=${2:?tag, e.g. v2026.11.12}
    work=$(mktemp -d); trap 'rm -rf "$work"' EXIT
    (cd "$work" && gh release download "$tag" --repo "${MSCOS_REPO:-mescon/mscOS}" --pattern '*.deb')
    (cd "$work" && sha256sum -- *.deb > SHA256SUMS)
    echo "Signing these packages of $tag:"; cat "$work/SHA256SUMS"
    sign "$work/SHA256SUMS"
    gh release upload "$tag" --repo "${MSCOS_REPO:-mescon/mscOS}" --clobber "$work/SHA256SUMS" "$work/SHA256SUMS.sig"
    echo "attached SHA256SUMS and SHA256SUMS.sig to $tag"
    ;;
verify)
    MSCOS_KEYS=$KEYS "$ROOT/core/mscos-verify" "${2:?file}" "${3:?signature}" && echo "good signature"
    ;;
*)
    sed -n '2,19p' "$0" | sed 's/^# \{0,1\}//'; exit 1
    ;;
esac
