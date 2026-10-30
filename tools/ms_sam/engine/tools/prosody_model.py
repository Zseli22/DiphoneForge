"""F0 model: FUN_5ed541ad (per-item pitch range) + Prosody_BuildPitchContour (FUN_5ed53c25)
+ gap fill (FUN_5ed53953 / FUN_5ed5389b / FUN_5ed53797) + knot sampling (FUN_5ed53b64).

Items are dicts with the fields the engine reads (offsets into its 0x154-byte phone item):
  type(+0) dur(+8) flags(+0xb4) accent(+0xb8) prom(+0xbc) bound(+0xc0) bstr(+0xc4)
  semi(+0xd8) off(+0x114) rng(+0x118)
"""
import struct

import numpy as np

SIL_TYPE = 0x11
ACC_ROUND = lambda x: x  # FUN_5ed53797 keeps its running value in an x87 register
f32 = np.float32
SEMI24 = [2.0 ** (k / 24.0) for k in range(25)]


def ftol(x):
    return int(np.trunc(x))


def item_from_bytes(b):
    i = lambda o: struct.unpack_from("<i", b, o)[0]
    f = lambda o: struct.unpack_from("<f", b, o)[0]
    return dict(type=i(0), dur=f(8), flags=i(0xB4), accent=i(0xB8), prom=i(0xBC), bound=i(0xC0),
                bstr=i(0xC4), semi=i(0xD8), off=f(0x114), rng=f(0x118))


def semitone_scale(semi, base):
    """FUN_5ed53757: base * 2^(semi/24), semi clamped to +-24 (table of float ratios)."""
    s = max(-24, min(24, semi))
    t = [f32(r) for r in SEMI24]
    return np.float64(base) * t[s] if s >= 0 else np.float64(base) / t[-s]


def pitch_range(it, base, prange):
    """per-item top (e8) and bottom (ec) in Hz."""
    a = f32(semitone_scale(it["semi"], base))
    lg = np.log2(np.float64(a) * 0.9784975170625504)  # FUN_5ed53736
    center_ext = lg + np.float64(f32(it["off"]))      # stays in an x87 register
    center = f32(center_ext)                          # FST float [ebp-8]
    half = f32(np.float64(prange) * np.float64(f32(it["rng"])))
    # FUN_5ed53adb takes a float argument: pow(2, f32(x)) * 1.021975
    top = f32(np.exp2(np.float64(f32(center_ext + np.float64(half)))) * 1.021975)
    bot = f32(np.exp2(np.float64(f32(np.float64(center) - np.float64(half)))) * 1.021975)
    return top, bot


def interp_gaps_linear(c):
    """FUN_5ed53797: fill runs of exact zeros by linear steps between the neighbours."""
    n = len(c)
    i0 = 0
    while i0 < n and c[i0] == 0.0:
        i0 += 1
    v0 = c[min(i0, n - 1)]
    last = n - 1
    j = last
    while j >= 0 and c[j] == 0.0:
        j -= 1
    vl = c[j]
    i = 0
    while i < last:
        while c[i] == 0.0:
            if last <= i:
                return
            k = i + 1
            while k < n and c[k] == 0.0:
                k += 1
            gap_end = k - 1  # last zero index
            nxt = k
            if gap_end < i:
                c[i] = c[gap_end + 1]
            else:
                if gap_end >= n or i >= n:
                    return
                prev = v0 if i == 0 else np.float64(c[i - 1])
                nv = vl if gap_end == last else np.float64(c[gap_end + 1])
                d = np.float64(nv) - prev
                L = gap_end - i
                acc = np.float64(prev)
                for m in range(i, gap_end + 1):
                    acc = acc + d / (L + 2)
                    acc = ACC_ROUND(acc)
                    c[m] = f32(acc)
            i = nxt
            if n <= nxt:
                return
        i += 1


def ease_fill(c, start, length, a, b, flag):
    """FUN_5ed5389b: quadratic ease between b and a (flag 0: rising, 1: falling)."""
    d = np.float64(a) - np.float64(b)
    half = int(length / 2) if length >= 0 else -int(-length / 2)
    for i in range(start, start + length):
        x = (i - start) / length
        if flag == 0:
            c[i] = f32(b + 2 * d * x * x) if i < start + half else f32((d + b) - 2 * d * (1 - x) ** 2)
        elif flag == 1:
            c[i] = f32(b + 2 * d * (1 - x) ** 2) if i >= start + half else f32((d + b) - 2 * d * x * x)


def smooth(c):
    """FUN_5ed53953: ease-fill zero gaps, one-pole smoothing, shift left by one frame."""
    n = len(c)
    i = 0
    while i < n and c[i] == 0.0:
        i += 1
    v0 = c[min(i, n - 1)]
    last = n - 1
    j = last
    while j >= 0 and c[j] == 0.0:
        j -= 1
    vl = c[j]
    i = 0
    while i < last:
        if c[i] == 0.0:
            k = i + 1
            while k < n and c[k] != 0.0 and False:
                pass
            k = i + 1
            while k < n and c[k] == 0.0:
                k += 1
            gap_end = k - 1
            if gap_end < i:
                c[i] = c[gap_end + 1]
            else:
                if gap_end >= n or i >= n:
                    break
                prev = v0 if i == 0 else c[i - 1]
                nxt = vl if gap_end == last else c[gap_end + 1]
                if np.float64(nxt) - np.float64(prev) <= 0.0:
                    ease_fill(c, i, gap_end - i + 1, prev, nxt, 1)
                else:
                    ease_fill(c, i, gap_end - i + 1, nxt, prev, 0)
            i = gap_end + 1
        else:
            i += 1
    if n > 1:
        y = np.float64(c[0])
        for k in range(n):
            y = np.float64(f32(0.9)) * y + np.float64(c[k]) * np.float64(f32(0.1))
            c[k] = f32(y)
        c[:-1] = c[1:].copy()
        c[-1] = c[-2]


def build(items, base, prange, stype):
    """Returns (contour, per-item (start,end,top,bot,t[20],f0[20]))."""
    n_items = len(items)
    # FUN_5ed541ad pass 1: frame spans (leading silences skipped, a trailing silence ignored)
    total = f32(0)
    frames = [(0, 0)] * n_items
    first = True
    prev_end = 0
    for k, it in enumerate(items):
        if first and it["type"] == SIL_TYPE:
            continue
        if k == n_items - 1 and it["type"] == SIL_TYPE:
            break
        tsum = np.float64(total) + np.float64(f32(it["dur"]))  # x87: fst stores a float, fmul uses the unrounded sum
        total = f32(tsum)
        end = ftol(tsum * 100.0)
        frames[k] = (prev_end, end)
        prev_end = end
        first = False
    ranges = [pitch_range(it, base, prange) for it in items]

    n = ftol(np.float64(total) * 100.0)
    top = np.zeros(n, f32)
    bot = np.full(n, f32(1e-5), f32)
    mid = np.zeros(n, f32)
    c = np.zeros(n, f32)
    if stype == 1:
        top[0], top[n - 1] = 1.0, f32(0.7)
    elif stype == 2:
        top[0], top[n - 1] = f32(0.9), 1.0
    elif stype == 0:
        top[0], top[n - 1] = 1.0, 1.0
    if stype in (0, 1, 2):
        interp_gaps_linear(top)
    k33 = np.float64(f32(0.33))
    mid[0] = f32((np.float64(top[0]) - bot[0]) * k33 + bot[0])
    mid[n - 1] = f32((np.float64(top[n - 1]) - bot[n - 1]) * k33 + bot[0])
    interp_gaps_linear(mid)
    c[0] = mid[0]
    c[n - 1] = f32(1e-4)

    T = lambda f: np.float64(top[f]); M = lambda f: np.float64(mid[f]); B = lambda f: np.float64(bot[f])
    idx = 0
    while idx < n_items and items[idx]["type"] == SIL_TYPE:
        idx += 1
    first = True
    counter = 1
    prev_start = prev_end = 0
    s = 0.0
    while idx < n_items - 1:
        it, nx = items[idx], items[idx + 1]
        start, end = frames[idx]
        nstart, nend = frames[idx + 1]
        ln = np.float64(f32(end - start))
        p = np.float64(it["prom"]) * np.float64(f32(0.1))
        A = it["accent"]
        if A == 1:
            if not first and prev_start != 0:
                f = ftol(ln * np.float64(f32(0.1)) + start)
                c[prev_start] = f32((T(f) - M(f)) * p * 0.25 + M(f))
            f = start if it["bound"] != 0 else ftol(ln * 0.5 + start)
            c[f] = f32((T(f) - M(f)) * p + M(f))
        elif A == 2:
            f = ftol(ln * np.float64(f32(0.3)) + start)
            c[f] = f32(M(f) - (M(f) - B(f)) * p)
        elif A == 3:
            c[start] = f32(M(start) - (M(start) - B(start)) * p)
            if nstart != 0:
                c[nend] = f32(M(nend) - (M(nend) - B(nend)) * p)
            s = 0.0
        elif A == 5:
            f = ftol(ln * np.float64(f32(0.3)) + start)
            if prev_start != 0:
                c[prev_start] = f32(M(prev_start) - (M(prev_start) - B(prev_start)) * (p * np.float64(f32(0.3))))
            c[f] = f32((T(f) - M(f)) * p + M(f))
            s = p
        elif A == 6:
            f = start
            if s == 0.0:
                c[f] = f32((T(f) - M(f)) * p + M(f))
                s = p
            else:
                s = np.float64(f32(s * 0.5))
                c[f] = f32((T(f) - M(f)) * s + M(f))
        elif A == 7:
            c[start] = f32((T(0) - M(0)) * p + M(0))
            f = ftol(ln * 0.75 + start)
            c[f] = f32(M(f) - (M(f) - B(f)) * p)
            s = p
        # boundary tones
        q = np.float64(it["bstr"]) * np.float64(f32(0.1))
        Bd = it["bound"]
        if Bd == 1000:
            c[end] = f32(M(end) - (M(end) - B(end)) * q)
        elif Bd == 1001:
            c[end] = f32((T(end) - M(end)) * q + M(end))
        elif Bd == 1002:
            c[start] = bot[end]
        elif Bd == 1003:
            mf = prev_start + int((prev_end - prev_start) / 2)
            c[mf] = f32(M(mf) - (M(mf) - B(mf)) * q)
            c[end] = f32((T(end) - M(end)) * q + M(end))
        elif Bd == 1004:
            c[end] = top[end]
        elif Bd == 1005:
            c[start] = f32((T(start) - M(start)) * q + M(start))
            c[end] = bot[end]
        if first and it["flags"] & 1:
            counter -= 1
            if counter < 0:
                first = False
        prev_start, prev_end = start, end
        idx += 1
    smooth(c)

    # FUN_5ed53afb (flat default) + FUN_5ed53b64 (knot sampling)
    out = []
    first = True
    for k, it in enumerate(items):
        topv, botv = ranges[k]
        step = np.float64(f32(it["dur"])) * np.float64(f32(0.05))
        t = [f32(step * j) for j in range(20)]
        flat = f32((np.float64(topv) - botv) * 0.5 + botv)
        f0 = [flat] * 20
        if k < n_items - 1 and not (first and it["type"] == SIL_TYPE):
            start, end = frames[k]
            R = np.float64(topv) - np.float64(botv)
            kk = 0.0
            f0, t = [], []
            for j in range(20):
                fr = ftol((end - start) * kk) + start
                f0.append(f32(R * np.float64(c[fr]) + botv))
                t.append(f32(kk * np.float64(f32(it["dur"]))))
                kk += np.float64(f32(0.05))
            first = False
        out.append(dict(frames=frames[k], top=topv, bot=botv, t=t, f0=f0))
    return c, out
