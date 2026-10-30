"""PitchMarks_Place (FUN_5ed42bda) model + segment record parsing."""
import struct
import numpy as np

f32 = np.float32


def seg_fields(b):
    n = struct.unpack_from("<i", b, 0x10)[0]
    return dict(
        unit=struct.unpack_from("<i", b, 0)[0],
        dur=struct.unpack_from("<f", b, 4)[0],
        hdr=struct.unpack_from("<2f", b, 8),
        n=n,
        t=np.frombuffer(b, "<f4", n, 0x14).copy(),
        f0=np.frombuffer(b, "<f4", n, 0x64).copy(),
        amp=np.frombuffer(b, "<f4", n, 0xB4).copy(),
    )


def seg_rate(dur, total_samples, sr=22050.0):
    return f32(np.float64(dur) * sr / total_samples)


def place_marks(periods, seg, rate, sr=22050.0, vib_amt=0.0, vib_state=0, vib_table=None, maxe=10**9):
    n, t, f0 = seg["n"], seg["t"], seg["f0"]
    frames, lens, flags = [], [], []
    f = 0; tsamp = 0; rep = 0; src_time = f32(0); T = 0.0
    inv = 1.0 / np.float64(rate)
    while f < len(periods) and len(frames) < maxe:
        p = periods[f]
        if p < 0:
            srclen = dst = int(np.trunc(-np.float64(p) + 0.5)); unv = True
        else:
            i = 1
            while i < n - 1 and not (tsamp <= t[i]):
                i += 1
            F = (tsamp - np.float64(t[i - 1])) * (np.float64(f0[i]) - f0[i - 1]) / (np.float64(t[i]) - t[i - 1]) + f0[i - 1]
            if vib_amt:
                F += (vib_table[(vib_state >> 16) & 0xFF] - 128) * vib_amt
            F = max(F, 10.0)
            srclen = int(np.trunc(np.float64(p) + 0.5)); dst = int(np.trunc(sr / F)); unv = False
            if vib_amt:
                vib_state = (vib_state - int(np.trunc(256.0 / (22050.0 / sr) * dst * -65536.0))) & 0xFFFFFF
        half = f32(inv * dst * 0.5)
        src_end = f32(src_time + f32(srclen))
        skipped = False
        if T + half <= src_end:
            flags.append(1 if (unv and rep & 1) else 0)
            tsamp += dst; T += inv * dst; rep += 1
            lens.append(dst); frames.append(f)
        else:
            skipped = True
        if T + half > src_end or skipped:
            f += 1; rep = 0; src_time = src_end
    return np.array(frames, int), np.array(lens, float), np.array(flags, int)
