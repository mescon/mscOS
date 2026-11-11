#!/usr/bin/env bash
# Run the tests in a Debian 13 container with the panel's dependencies (needs Docker), or
# directly where python3-aiohttp, python3-websockets and python3-numpy are installed:
#   python3 -m unittest discover -s tests -t . -v
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
exec docker run --rm -v "$ROOT:/src:ro" -w /src debian:trixie-slim bash -c '
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq && apt-get install -y -qq --no-install-recommends python3 python3-aiohttp python3-websockets python3-numpy >/dev/null
    cp -r /src /work && cd /work && python3 -m unittest discover -s tests -t . -v'
