"""Objective comparison of voice renders: long-term spectrum distance to a reference, and periodicity.

Two renders of the same text by different engines don't line up in time, so frame-by-frame measures are
meaningless here.  These two aren't: the long-term average spectrum (does the voice have the same timbre
as the reference) and the harmonic-to-noise ratio (how buzzy or noisy the excitation is).

usage: python voicecheck.py ref.wav other.wav [more.wav ...]
"""
import sys
import wave

import numpy as np
from scipy.signal import resample_poly
from math import gcd


def read(p, rate=16000):
    with wave.open(p, "rb") as w:
        x = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float64)
        r = w.getframerate()
    if r != rate:
        g = gcd(rate, r)
        x = resample_poly(x, rate // g, r // g)
    return x


def ltas(x, n=512, hop=160):
    """long-term average spectrum in dB, energy-normalized, loud frames only"""
    T = max((len(x) - n) // hop, 1)
    win = np.hanning(n)
    S = np.array([np.abs(np.fft.rfft(x[t * hop:t * hop + n] * win)) ** 2 for t in range(T)])
    e = S.sum(1)
    S = S[e > np.percentile(e, 60)]
    m = 10 * np.log10(S.mean(0) + 1e-9)
    return m - m.max()


def hnr(x, rate=16000, n=1024, hop=256):
    """median harmonic-to-noise ratio over voiced frames, via the autocorrelation peak"""
    out = []
    win = np.hanning(n)
    for t in range((len(x) - n) // hop):
        seg = x[t * hop:t * hop + n] * win
        if np.sqrt((seg ** 2).mean()) < 200:
            continue
        seg = seg - seg.mean()
        ac = np.correlate(seg, seg, "full")[n - 1:]
        if ac[0] <= 0:
            continue
        lo, hi = rate // 400, rate // 60
        r = ac[lo:hi].max() / ac[0]
        if r > 0.3:
            out.append(10 * np.log10(r / max(1 - r, 1e-6)))
    return float(np.median(out)) if out else float("nan")


def main():
    ref = read(sys.argv[1])
    R = ltas(ref)
    print("%-22s %-28s %s" % ("file", "spectrum distance to ref", "harmonic-to-noise"))
    print("%-22s %-28s %6.1f dB" % (sys.argv[1].split("/")[-1].split("\\")[-1], "(reference)", hnr(ref)))
    for f in sys.argv[2:]:
        x = read(f)
        d = float(np.sqrt(np.mean((ltas(x) - R) ** 2)))
        print("%-22s %-28.2f %6.1f dB" % (f.split("/")[-1].split("\\")[-1], d, hnr(x)))


if __name__ == "__main__":
    main()
