#!/usr/bin/env bash
# Record what PipeWire sends to the USB DAC for N seconds and count digital-silence gaps.
# Usage: gapcheck.sh [seconds] [host]
set -euo pipefail
SECS=${1:-30}; HOST=${2:-root@192.168.1.200}
OUT=$(mktemp --suffix=.wav)
trap 'rm -f "$OUT"' EXIT
ssh "$HOST" "U=\$(id -u soloist); S=\$(runuser -u soloist -- env XDG_RUNTIME_DIR=/run/user/\$U pw-dump | grep -o '\"alsa_output.usb-[^\"]*' | head -1 | tr -d '\"');
  runuser -u soloist -- env XDG_RUNTIME_DIR=/run/user/\$U timeout $SECS pw-record -P '{ stream.capture.sink=true }' --target \$S --rate 44100 --channels 2 --format f32 /tmp/gapcheck.wav 2>/dev/null || true"
scp -q "$HOST:/tmp/gapcheck.wav" "$OUT"
python3 -I - "$OUT" <<'EOF'
import sys, struct, numpy as np
d=open(sys.argv[1],'rb').read(); i=d.find(b'data'); n=struct.unpack('<I',d[i+4:i+8])[0]
x=np.frombuffer(d[i+8:i+8+n],dtype='<f4'); x=x[:len(x)//2*2].reshape(-1,2)
z=np.all(x==0,axis=1); e=np.diff(np.concatenate([[0],z.astype(int),[0]]))
st=np.where(e==1)[0]; en=np.where(e==-1)[0]; L=en-st
gaps=[(round(s/44100,2),int(l)) for s,l in zip(st,L) if l>=64 and s>44100]  # skip first second (capture start)
rms=float(np.sqrt((x**2).mean()))
print(f"secs={len(x)/44100:.1f} rms={rms:.3f} gaps={len(gaps)} lost_ms={sum(l for _,l in gaps)/44.1:.0f} first={gaps[:6]}")
if rms < 1e-4: print("WARNING: capture is silent - is anything playing?")
EOF
