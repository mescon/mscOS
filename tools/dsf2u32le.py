"""DSF (stereo, 1-bit LSB-first) -> raw ALSA DSD_U32_LE interleaved, a time window."""
import struct, sys
import numpy as np
src, dst, start_s, secs = sys.argv[1], sys.argv[2], float(sys.argv[3]), float(sys.argv[4])
with open(src, "rb") as f:
    d = f.read()
assert d[:4] == b"DSD " and d[28:32] == b"fmt "
fmt = d[28:80]
ch, fs, bits, nsamp, block = (struct.unpack_from("<I", fmt, o)[0] for o in (24, 28, 32, 36, 44))
assert ch == 2 and bits == 1, (ch, bits)
data_off = 80 + 12
data_len = struct.unpack_from("<Q", d, 84)[0] - 12
raw = np.frombuffer(d, np.uint8, count=data_len, offset=data_off)
nblk = len(raw) // (block * 2)
raw = raw[: nblk * block * 2].reshape(nblk, 2, block)
L = raw[:, 0, :].reshape(-1); R = raw[:, 1, :].reshape(-1)
rev = np.array([int(f"{i:08b}"[::-1], 2) for i in range(256)], np.uint8)  # LSB-first -> MSB-first
bps = fs // 8  # bytes per second per channel
a, n = int(start_s * bps) // 4 * 4, int(secs * bps) // 4 * 4
L, R = rev[L[a:a + n]], rev[R[a:a + n]]
# DSD_U32_LE: per channel 4 bytes in time order b0..b3 -> u32 value b0<<24|..|b3, stored LE = b3 b2 b1 b0
Lw = L.reshape(-1, 4)[:, ::-1]; Rw = R.reshape(-1, 4)[:, ::-1]
out = np.stack([Lw, Rw], axis=1).reshape(-1)
pad = np.full(fs // 8 * 2 // 2, 0x69, np.uint8)  # ~0.5 s DSD silence first
open(dst, "wb").write(pad.tobytes() + out.tobytes())
print(f"fs={fs} alsa_rate={fs // 32} bytes={len(out)} seconds={n / bps:.1f}")
