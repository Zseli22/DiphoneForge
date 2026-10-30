"""Concatenate WAVs of different sample rates into one A/B file (resampling to the highest rate).

usage: python abcat.py out.wav a.wav b.wav ...
"""
import sys
import wave
from math import gcd

import numpy as np
from scipy.signal import resample_poly


def read(p):
    with wave.open(p, "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float64), w.getframerate()


def main():
    out, files = sys.argv[1], sys.argv[2:]
    parts = [read(f) for f in files]
    rate = max(r for _, r in parts)
    pieces = []
    for x, r in parts:
        if r != rate:
            g = gcd(rate, r)
            x = resample_poly(x, rate // g, r // g)
        pieces += [x, np.zeros(int(0.5 * rate))]
    y = np.clip(np.concatenate(pieces), -32768, 32767).astype("<i2")
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(y.tobytes())
    print("%s: %d clips at %d Hz (%.1f s)" % (out, len(files), rate, len(y) / rate))


if __name__ == "__main__":
    main()
