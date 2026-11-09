"""Analyse captured float samples sent to the DAC.

Spotify Lossless is FLAC (16 or 24 bit). Decoded to float, every sample is k / 2^(bits-1).
If Soloist applies a gain (volume < 100 %, loudness normalisation), samples become
k * g / 2^(bits-1): still a lattice, but with a step g. Lossy (Ogg/AAC) audio lands on no
lattice at all. Finding the lattice therefore tells us source bit depth and applied gain.
"""
from __future__ import annotations

import math
import struct

import numpy as np


def read_f32_wav(path: str) -> np.ndarray:
    data = open(path, "rb").read()
    i = data.find(b"data")
    if i < 0:
        raise ValueError("no data chunk")
    n = struct.unpack("<I", data[i + 4:i + 8])[0]
    x = np.frombuffer(data[i + 8:i + 8 + n], dtype="<f4")
    return x[: len(x) // 2 * 2].reshape(-1, 2)


def find_gaps(x: np.ndarray, rate: int = 44100, skip_s: float = 1.0, min_frames: int = 64):
    """Runs of exact digital silence on both channels (dropouts), ignoring capture start."""
    z = np.all(x == 0, axis=1)
    e = np.diff(np.concatenate([[0], z.astype(np.int8), [0]]))
    starts, ends = np.where(e == 1)[0], np.where(e == -1)[0]
    gaps = [(round(s / rate, 2), int(t - s)) for s, t in zip(starts, ends)
            if t - s >= min_frames and s > skip_s * rate and t < len(z)]
    return gaps


def on_grid(v: np.ndarray, step: float, tol: float = 1e-3) -> float:
    s = v / step
    return float(np.mean(np.abs(s - np.round(s)) < tol))


def lattice(x: np.ndarray) -> dict:
    """Return {'bits', 'gain_db', 'on_grid', 'verdict'} for the captured programme."""
    v = x.astype(np.float64).ravel()
    v = v[v != 0]
    if v.size < 10000:
        return {"verdict": "silent", "bits": None, "gain_db": None, "on_grid": 0.0}
    v = v[: 400000]
    for bits in (16, 24):  # unity gain first: the bit-perfect case
        lsb = 2.0 ** -(bits - 1)
        if on_grid(v, lsb) > 0.995:
            return {"verdict": "bitperfect", "bits": bits, "gain_db": 0.0, "on_grid": on_grid(v, lsb)}
    # Look for a scaled lattice. After an arbitrary gain, k*g/2^23 is rounded to float32
    # (24-bit mantissa), so only small samples keep the lattice exactly: use |x| < 2^-7 (rounding error < 0.01 step).
    small = v[np.abs(v) < 2.0 ** -7]
    if small.size < 500:
        return {"verdict": "inconclusive", "bits": None, "gain_db": None, "on_grid": 0.0}
    v = small
    mags = np.unique(np.abs(v))[:64]
    best = (0.0, None)
    for m in mags[:16]:
        for k in range(1, 33):
            step = m / k
            score = on_grid(v[:50000], step, tol=1e-2)
            if score > best[0] + 1e-9 or (abs(score - best[0]) < 1e-9 and best[1] and step > best[1]):
                best = (score, step)
            if score > 0.999:
                break
        if best[0] > 0.999:
            break
    score, step = best
    if step and score > 0.99:
        full = on_grid(v, step, tol=1e-2)
        # Express the step relative to 24- and 16-bit LSBs; the gain is that ratio.
        for bits in (24, 16):
            g = step / 2.0 ** -(bits - 1)
            if 0.05 < g <= 1.0:
                return {"verdict": "scaled", "bits": bits, "gain_db": round(20 * math.log10(g), 2),
                        "on_grid": full}
    return {"verdict": "lossy_or_processed", "bits": None, "gain_db": None, "on_grid": score}


if __name__ == "__main__":
    import json
    import sys
    x = read_f32_wav(sys.argv[1])
    out = lattice(x[44100:])
    out["gaps"] = find_gaps(x)
    out["seconds"] = round(len(x) / 44100, 1)
    print(json.dumps(out))
