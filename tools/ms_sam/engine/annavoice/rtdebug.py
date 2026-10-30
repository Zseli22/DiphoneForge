"""Which step of the encoding adds the high band?  Rebuild one instance four ways and compare spectra.

  direct   : residual through 1/A(z) with the analysis LPC        (should be nearly the original)
  lsfroute : same, but A(z) rebuilt from the LSFs                 (isolates the LSF conversion)
  packed   : the residual taken through the stored spectrum       (isolates the pulse packing)
  aligned  : as stored, with the pulse rotation                   (isolates the phase alignment)
"""
import numpy as np

import spdbuild as B
from roundtrip import lsf_to_lpc, unpack_spectrum
from voicecheck import ltas

BANDS = [(0, 300), (300, 800), (800, 1500), (1500, 2500), (2500, 4000), (4000, 6000), (6000, 8000)]


def allpole(a, exc, mem):
    y = np.zeros(len(exc))
    p = len(a) - 1
    for k in range(len(exc)):
        acc = exc[k] - float(np.dot(mem[:p], a[1:]))
        mem[1:p] = mem[:p - 1]
        mem[0] = acc
        y[k] = acc
    return y


def main():
    c = B.Corpus("corpus")
    z = np.load("corpus/segments.npz", allow_pickle=True)
    seg = {int(u): np.asarray(b).reshape(-1, 2) for u, b in zip(z["unit"], z["bounds"])}
    lag = np.exp(-0.5 * (2 * np.pi * 60.0 / B.RATE * np.arange(21)) ** 2)
    rng = np.random.default_rng(5)
    keys = sorted(seg)
    src, outs = [], {k: [] for k in ("direct", "lsfroute", "packed", "aligned")}
    lsf_err = []
    for i in rng.choice(len(keys), size=80, replace=False):
        u = keys[int(i)]
        b = seg[u]
        j = int(rng.integers(0, len(b)))
        s0 = int(b[j][0]) * B.HOP + B.WIN // 2
        s1 = min(int(b[j][1]) * B.HOP + B.WIN // 2, int(c.u[u]["len"]))
        if s1 - s0 < 320:
            continue
        x = c.audio(u)
        ep = c.epochs(u)
        if len(ep) == 0:
            continue
        marks = np.concatenate([[0], np.cumsum(np.abs(ep))])
        mem = {k: np.zeros(20) for k in outs}
        piece = []
        for k in range(len(ep)):
            s, n = int(marks[k]), int(abs(ep[k]))
            if n < 16 or n > 800 or s < s0 or s + n > min(s1, len(x)) or ep[k] <= 0:
                continue
            r = B.analyse_period(x, s, n, 20, lag)
            if r is None:
                continue
            lsf, res = r
            a_direct = None
            # the analysis filter, recovered from the LSFs (what the file actually stores)
            a_lsf = lsf_to_lpc(lsf)
            lsf2 = B.lpc_to_lsf(a_lsf)
            if lsf2 is not None:
                lsf_err.append(np.abs(np.sort(lsf2) - np.sort(lsf)).max())
            piece.append(x[s:s + n])
            outs["direct"].append(allpole(a_lsf, res, mem["direct"]))
            outs["lsfroute"].append(allpole(a_lsf, res, mem["lsfroute"]))
            for name, align in (("packed", False), ("aligned", True)):
                spec = B.pack_spectrum(res, 512, align)
                g = float(np.sqrt(np.mean(spec ** 2)))
                buf = unpack_spectrum(spec / max(g, 1e-9)) * g
                exc = np.zeros(n)                     # exactly sam.c's wrap of the centred pulse
                m = min(n, 256)
                exc[:m] = buf[:m]
                for t in range(n - m, n):
                    exc[t] += buf[t - n + 512]
                outs[name].append(allpole(a_lsf, exc, mem[name]))
        if piece:
            src.append(np.concatenate(piece))
    X = np.concatenate(src)
    f = np.fft.rfftfreq(512, 1.0 / B.RATE)
    A = ltas(X)
    print("LSF round-trip error: max %.2e" % (max(lsf_err) if lsf_err else 0))
    print("%-12s" % "band Hz" + "".join("%10s" % k for k in outs))
    rows = {k: ltas(np.concatenate(v)) for k, v in outs.items() if v}
    for lo, hi in BANDS:
        m = (f >= lo) & (f < hi)
        print("%5d-%5d %6.1f" % (lo, hi, A[m].mean()) + "".join("%+10.1f" % (rows[k][m].mean() - A[m].mean()) for k in outs))


if __name__ == "__main__":
    main()
