"""Export the project recordings as an ANCP corpus for the Microsoft Sam voice builder.

One corpus unit per phone instance.  Units inside a line are contiguous, so the builder sees the real
left/right context; lines are separated by unowned silence, so context stops at the line edge.  Audio is
16-bit mono at --rate; epochs are pitch periods in samples, positive = voiced, negative = unvoiced.

    python corpus_build.py --out build/ms_sam/corpus --rate 22050

The phone boundaries come from the MFA TextGrids and their labels are ARPAbet with stress, which is
already the phone set in Sam.spd, so no name translation is needed.
"""
import argparse
import glob
import os
import wave

import numpy as np
from scipy.signal import resample_poly

from textgrid import read

RATE = 22050
HOP = 110        # 5 ms
WIN = 550        # 25 ms
MAXP = 552       # longest period stored (40 Hz at 22.05 kHz)
VOICELESS = {"P", "T", "K", "F", "TH", "S", "SH", "CH", "HH"}
VOWELS = {"AA", "AE", "AH", "AO", "AW", "AY", "EH", "ER", "EY", "IH", "IY", "OW", "OY", "UH", "UW"}

REC = np.dtype([("type", "<i4"), ("start", "<i4"), ("len", "<i4"), ("pcm_off", "<i8"),
                ("nep", "<i4"), ("ep_off", "<i8"), ("feat", "u1", 9), ("pad", "u1", 3)])


def load_wav(path):
    with wave.open(path) as w:
        assert w.getsampwidth() == 2, path
        x = np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(np.float64)
        if w.getnchannels() == 2:
            x = x.reshape(-1, 2).mean(1)
        rate = w.getframerate()
    if rate != RATE:
        x = resample_poly(x, RATE, rate)
    return x


def f0_track(x, lo_hz=69, hi_hz=413, thresh=0.42):
    """normalised autocorrelation pitch, one value per 5 ms frame; 0 = unvoiced"""
    n = 1 + max(0, (len(x) - WIN) // HOP)
    if n <= 0 or len(x) < WIN:
        return np.zeros(0), 0
    idx = np.arange(WIN)[None, :] + HOP * np.arange(n)[:, None]
    seg = x[idx] * np.hanning(WIN)
    seg = seg - seg.mean(1, keepdims=True)
    S = np.fft.rfft(seg, 2 * WIN, axis=1)
    ac = np.fft.irfft(S * np.conj(S), 2 * WIN, axis=1)[:, :WIN]
    e0 = ac[:, 0].copy()
    e0[e0 < 1e-9] = 1e-9
    lags = np.arange(max(2, int(round(RATE / hi_hz))), min(WIN - 1, int(round(RATE / lo_hz))))
    r = ac[:, lags] / e0[:, None]
    best = r.argmax(1)
    peak = r[np.arange(n), best]
    lag = lags[best].astype(np.float64)
    i = np.clip(best, 1, len(lags) - 2)
    rr, a, b = r[np.arange(n), i], r[np.arange(n), i - 1], r[np.arange(n), i + 1]
    den = a - 2 * rr + b
    with np.errstate(divide="ignore", invalid="ignore"):
        delta = np.where(np.abs(den) > 1e-9, 0.5 * (a - b) / den, 0.0)
    lag = lag + np.clip(delta, -1, 1)
    return np.where(peak > thresh, RATE / lag, 0.0), n


def smooth_f0(f0):
    """fill short unvoiced gaps and drop one-frame octave jumps"""
    if len(f0) == 0:
        return f0
    g = f0.copy()
    for i in range(len(g)):
        if g[i] <= 0:
            prev = next((g[j] for j in range(i - 1, max(-1, i - 4), -1) if g[j] > 0), 0)
            nxt = next((g[j] for j in range(i + 1, min(len(g), i + 4)) if g[j] > 0), 0)
            if prev and nxt:
                g[i] = (prev + nxt) / 2
    for i in range(1, len(g) - 1):
        if g[i - 1] > 0 and (g[i] > 1.7 * g[i - 1] or g[i] < 0.6 * g[i - 1]):
            g[i] = g[i - 1]
    return g


def const_epochs(n, step):
    out, t = [], 0
    while t < n:
        p = min(abs(step), n - t)
        if p < 16:
            break
        out.append(int(np.sign(step) * p))
        t += p
    return np.array(out or [-max(16, min(n, MAXP))], "<i2")


def build_epochs(n, f0, nf):
    out, t = [], 0
    while t < n:
        k = min(t // HOP, max(nf - 1, 0))
        p = int(round(RATE / f0[k])) if nf and f0[k] > 0 else -HOP
        p = int(np.clip(p, -MAXP, MAXP))
        p = max(p, 16) if p > 0 else min(p, -16)
        if t + abs(p) > n:                   # the last period must fit inside the unit
            break
        out.append(p)
        t += abs(p)
    return np.array(out or [-max(16, min(n, MAXP))], "<i2")


def fill_vowel(f0):
    """A vowel is voiced from end to end: interpolate across detector dropouts and extend to the edges."""
    if len(f0) == 0 or not np.any(f0 > 0):
        return f0
    idx = np.where(f0 > 0)[0]
    a, b = int(idx[0]), int(idx[-1])
    g = f0.copy()
    g[a:b + 1] = np.interp(np.arange(a, b + 1), idx, f0[idx])
    g[:a] = g[a]
    g[b + 1:] = g[b]
    return g


def epochs_for(x, label=""):
    """period lengths that tile x, signed by voicing; the phone class decides what may be voiced"""
    if label in VOICELESS or label in ("SIL", "+NOISE+", ""):
        return const_epochs(len(x), -HOP)
    if label[:-1] in VOWELS and label[-1].isdigit():
        f0, n = f0_track(x, 70, 228, 0.35)       # the speaker's own pitch range
        if not np.any(f0 > 0):
            f0, n = f0_track(x, 55, 320, 0.25)
        f0 = fill_vowel(f0)
    else:
        f0, n = f0_track(x, 55, 320, 0.42)       # a voiced consonant
    return build_epochs(len(x), smooth_f0(f0), n)


def stress_of(label):
    return int(label[-1]) if label and label[-1].isdigit() else 0


def normalize_label(label):
    """MFA's ARPA set has secondary stress and unstressed diphthongs; Sam has neither."""
    if label in ("spn", ""):
        return None
    if len(label) > 1 and label[-1] == "2":
        label = label[:-1] + "1"
    if label in ("AY0", "EY0", "OW0", "AW0", "OY0"):
        label = label[:-1] + "1"
    return label


def main():
    global RATE, HOP, WIN, MAXP
    ap = argparse.ArgumentParser()
    ap.add_argument("--segments", default=os.path.join("output", "segments"))
    ap.add_argument("--textgrids", default=os.path.join("output", "mfa_aligned"))
    ap.add_argument("--out", required=True)
    ap.add_argument("--rate", type=int, default=22050)
    ap.add_argument("--gap", type=float, default=0.35, help="silence between lines, seconds")
    ap.add_argument("--skip", help="file of basenames to leave out (one per line)")
    args = ap.parse_args()
    RATE = args.rate
    HOP = int(round(RATE * 0.005))
    WIN = int(round(RATE * 0.025))
    MAXP = int(round(RATE / 40))

    os.makedirs(args.out, exist_ok=True)
    skip = set()
    if args.skip:
        skip = {l.strip() for l in open(args.skip) if l.strip()}
    wavs = sorted(glob.glob(os.path.join(args.segments, "*.wav")))
    types, recs, pcm, eps = {}, [], [], []
    pcm_off = ep_off = 0
    gap = np.zeros(int(round(args.gap * RATE)), np.int16)
    gap_written = False
    nphones = 0
    for k, wp in enumerate(wavs):
        base = os.path.splitext(os.path.basename(wp))[0]
        if base in skip:
            continue
        tg = os.path.join(args.textgrids, base + ".TextGrid")
        if not os.path.exists(tg):
            continue
        phones = [(a, b, s.strip()) for a, b, s in read(tg).get("phones", []) if s.strip()]
        if not phones:
            continue
        x = load_wav(wp)
        if gap_written:
            pcm.append(gap)
            pcm_off += len(gap)
        gap_written = True
        for a, b, lab in phones:
            lab = normalize_label(lab)
            if lab is None:
                continue
            i0 = max(0, int(round(a * RATE)))
            i1 = min(len(x), int(round(b * RATE)))
            if i1 - i0 < 200:
                continue
            seg = x[i0:i1]
            ep = epochs_for(seg, lab)
            if lab not in types:
                types[lab] = len(types)
            sig = np.clip(np.round(seg), -32768, 32767).astype("<i2")
            r = np.zeros(1, dtype=REC)[0]
            r["type"] = types[lab]
            r["start"] = pcm_off
            r["len"] = len(sig)
            r["pcm_off"] = pcm_off
            r["nep"] = len(ep)
            r["ep_off"] = ep_off
            r["feat"][7] = stress_of(lab)
            recs.append(r)
            pcm.append(sig)
            eps.append(ep)
            pcm_off += len(sig)
            ep_off += len(ep)
            nphones += 1
        if k % 200 == 0:
            print("  %d/%d lines, %d phones" % (k, len(wavs), nphones))

    u = np.array(recs, dtype=REC)
    audio = np.concatenate(pcm).astype("<i2")
    epochs = np.concatenate(eps).astype("<i2")
    with open(os.path.join(args.out, "units.bin"), "wb") as f:
        f.write(b"ANCP" + np.array([1, len(u)], "<u4").tobytes() + u.tobytes())
    with open(os.path.join(args.out, "audio.pcm"), "wb") as f:
        f.write(audio.tobytes())
    with open(os.path.join(args.out, "epochs.i16"), "wb") as f:
        f.write(epochs.tobytes())
    with open(os.path.join(args.out, "types.txt"), "w") as f:
        for name, i in sorted(types.items(), key=lambda kv: kv[1]):
            f.write("%d %s\n" % (i, name))

    # each unit is one phone and is exactly its own audio, so its alignment is the whole unit
    bounds = [np.array([[0, max(2, int(round(int(r["len"]) / HOP)))]], np.int32) for r in u]
    np.savez_compressed(os.path.join(args.out, "segments.npz"),
                        unit=np.arange(len(u)), bounds=np.array(bounds, dtype=object),
                        allow_pickle=True)
    print("units %d, %.1f s audio, %d types -> %s"
          % (len(u), len(audio) / RATE, len(types), args.out))


if __name__ == "__main__":
    main()
