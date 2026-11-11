#!/usr/bin/env bash
# Send a command to Soloist's local WebSocket API on the device and print the replies.
# Usage: solo.sh <command> [json-extra]   e.g. solo.sh play ; solo.sh get_state
set -euo pipefail
CMD=${1:?command}; EXTRA=${2:-}
HOST=${HOST:-${MSCOS_HOST:-root@mscos.local}}
ssh "$HOST" "python3 - $(printf %q "$CMD") $(printf %q "${EXTRA:-{\}}")" <<'EOF'
import asyncio, json, sys, websockets
cmd, extra = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else "")
async def main():
    async with websockets.connect("ws://127.0.0.1:9090", max_size=None) as ws:
        msg = {"type": "command", "command": cmd}
        if extra: msg.update(json.loads(extra))
        await ws.send(json.dumps(msg))
        try:
            while True:
                r = json.loads(await asyncio.wait_for(ws.recv(), 2))
                t = r.get("type")
                if t == "playback_state":
                    it = (r.get("item") or {}).get("decorations", {})
                    print("state:", r.get("status"), "|", it.get("identity", {}).get("name"), "| vol", r.get("volume"))
                else:
                    print(t, {k: v for k, v in r.items() if k not in ("type", "item", "context")})
        except asyncio.TimeoutError:
            pass
asyncio.run(main())
EOF
