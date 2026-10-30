"""Encode-decode round trip of single phone instances, to find where the extra high band comes from.

It runs the same analysis the builder runs, reconstructs the audio exactly as Sam's engine would (pulse
from the stored spectrum, scaled by the gain, through the all-pole filter) and compares the long-term
spectrum with the source.  Any difference is the encoding's fault, not the front end's.

usage: python roundtrip.py [--corpus DIR] [--n 200] [--no-align] [--order 20]
"""
import argparse

import numpy as np

import spdbuild as B


def lsf_to_lpc(lsf):
    """LSF (0..0.5) -> a[0..p], y[n] = x[n] - sum a[k] y[n-k]  (same convention as sam.c lsf_to_lpc)"""
    p = len(lsf)
    w = 2 * np.pi * np.sort(np.asarray(lsf, dtype=np.float64))
    P = np.array([1.0])
    Q = np.array([1.0])
    for i in range(0, p, 2):
        P = np.convolve(P, [1.0, -2.0 * np.cos(w[i]), 1.0])
    for i in range(1, p, 2):
        Q = np.convolve(Q, [1.0, -2.0 * np.cos(w[i]), 1.0])
    P = np.convolve(P, [1.0, 1.0])
    Q = np.convolve(Q, [1.0, -1.0])
    a = 0.5 * (P[:p + 1] + Q[:p + 1])
    a[0] = 1.0
    return a


def unpack_spectrum(packed, nfft=512):
    X = np.zeros(nfft // 2 + 1, dtype=complex)
    X.real = packed[:nfft // 2 + 1]
    X.imag[1:nfft // 2] = packed[nfft // 2 + 1:][::-1]
    return np.fft.irfft(X, nfft)


def rebuild(frames, noise, nfft=512):
    """frames -> audio, the way sam.c does it (pulse wrapped into the period, then the all-pole filter)"""
    out = []
    mem = np.zeros(21)
    npos = 0
    for (n, v, lsf, g, spec) in frames:
        a = lsf_to_lpc(lsf)
        exc = np.zeros(n)
        if v and spec is not None:
            buf = unpack_spectrum(np.asarray(spec, dtype=np.float64)) * g
            m = min(n, nfft // 2)
            exc[:m] = buf[:m]
            if n > m:
                exc[m:] += buf[nfft - (n - m):]
        else:
            exc = g * 0.02 * noise[npos:npos + n]
            npos = (npos + n) % (len(noise) - 1000)
        y = np.zeros(n)
        for k in range(n):
            acc = exc[k] - float(np.dot(mem[1:len(a)], a[1:]))
            mem[1:] = np.roll(mem[1:], 1)
            mem[1] = acc
            y[k] = acc
        out.append(y)
    return np.concatenate(out) if out else np.zeros(0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default="corpus")
    ap.add_argument("--n", type=int, default=120)
    ap.add_argument("--no-align", action="store_true")
    ap.add_argument("--order", type=int, default=20)
    a = ap.parse_args()

    c = B.Corpus(a.corpus)
    z = np.load(a.corpus + "/segments.npz", allow_pickle=True)
    seg = {int(u): np.asarray(b).reshape(-1, 2) for u, b in zip(z["unit"], z["bounds"])}
    lag = np.exp(-0.5 * (2 * np.pi * 60.0 / B.RATE * np.arange(a.order + 1)) ** 2)
    rng = np.random.default_rng(3)
    noise = rng.standard_normal(B.RATE)

    src, out = [], []
    keys = sorted(seg)
    for i in rng.choice(len(keys), size=min(a.n, len(keys)), replace=False):
        u = keys[int(i)]
        b = seg[u]
        j = rng.integers(0, len(b))
        s0 = int(b[j][0]) * B.HOP + B.WIN // 2
        s1 = min(int(b[j][1]) * B.HOP + B.WIN // 2, int(c.u[u]["len"]))
        if s1 - s0 < 320:
            continue
        old = B.pack_spectrum
        if a.no_align:
            B.pack_spectrum = lambda r, nfft=512, align=True: old(r, nfft, False)
        fr = B.analyse_instance(c, u, s0, s1, lag, 48)
        B.pack_spectrum = old
        if not fr:
            continue
        x = c.audio(u)
        marks = np.concatenate([[0], np.cumsum(np.abs(c.epochs(u)))])
        src.append(x[s0:s1])
        out.append(rebuild(fr, noise))
    X = np.concatenate(src)
    Y = np.concatenate(out)
    print("%d instances, %.1f s source, %.1f s rebuilt" % (len(src), len(X) / B.RATE, len(Y) / B.RATE))
    f = np.fft.rfftfreq(512, 1.0 / B.RATE)
    from voicecheck import ltas
    A, C = ltas(X), ltas(Y)
    print("band Hz        source   rebuilt   diff")
    for lo, hi in [(0, 300), (300, 800), (800, 1500), (1500, 2500), (2500, 4000), (4000, 6000), (6000, 8000)]:
        m = (f >= lo) & (f < hi)
        print("%5d-%5d  %8.1f  %8.1f  %+6.1f" % (lo, hi, A[m].mean(), C[m].mean(), C[m].mean() - A[m].mean()))


if __name__ == "__main__":
    main()
