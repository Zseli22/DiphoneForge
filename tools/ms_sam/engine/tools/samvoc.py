"""Python reference model of Microsoft Sam's (SAPI5 spttseng.dll) back end.

Each function names the engine routine it mirrors. This is the prototype the C port is checked against.
"""
import struct
import wave

import numpy as np

DEFAULT_SPD = r"C:\Program Files (x86)\Common Files\SpeechEngines\Microsoft\TTS\1033\sam.spd"
TWO_PI = np.float32(6.2831855)  # DAT_5ede9a44, set to 0x40c90fdb at startup


class Voice:
    """Voice file loader (FUN_5ed58d7a). Only the synth section (#3) is interpreted so far."""

    def __init__(self, path=DEFAULT_SPD):
        d = open(path, "rb").read()
        assert d[:4] == b"Data" and struct.unpack_from("<I", d, 4)[0] == 0x10000
        self.data = d
        self.sections = [struct.unpack_from("<II", d, 0x18 + i * 8) for i in range(5)]
        self.base = b = self.sections[3][0]
        h = lambda o: struct.unpack_from("<i", d, b + o)[0]
        self.rate = h(0)
        self.order = h(0x498)
        self.fft_n = h(0x49C)
        self.fft_log2 = h(0x4A0)
        self.n_units = h(0x490)

        def codebooks(count_off, table_off):
            out = []
            for i in range(h(count_off)):
                dim, off = h(table_off + i * 12), h(table_off + 4 + i * 12)
                n = h(table_off + 8 + i * 12)
                # the last table entry stores 0 entries; size runs to the next codebook, assume same as others
                out.append((dim, b + off))
            return out

        self.lsf_cb = codebooks(4, 0x14)
        self.exc_cb = codebooks(8, 0x194)
        self.dexc_cb = codebooks(0xC, 0x314)
        self.unit_off = np.frombuffer(d, "<i4", self.n_units, b + h(0x494))
        self.sine = np.frombuffer(d, "<f4", 2 * self.fft_n, b + h(0x4A4)).copy()
        self.window = np.frombuffer(d, "<f4", self.fft_n, b + h(0x4A8)).copy()
        self.noise = np.frombuffer(d, "<f4", self.rate, b + h(0x4AC)).copy()
        self.noise_pos = 0  # voice object +0x78, persists across units

    def cb_vec(self, cb, idx):
        dim, off = cb
        return np.frombuffer(self.data, "<f4", dim, off + idx * dim * 4)


def read_escaped(d, o, n):
    """FUN_5ed57ea2: signed bytes, 0x7F/0x80 continue into the next byte."""
    out = np.empty(n, np.float32)
    for k in range(n):
        c = struct.unpack_from("b", d, o)[0]
        if c in (127, -128):
            v = float(c)
            while struct.unpack_from("b", d, o)[0] == c:
                o += 1
                v += struct.unpack_from("b", d, o)[0]
        else:
            v = float(c)
        out[k] = v
        o += 1
    return out, o


def lsf_to_lpc(lsf, order):
    """FUN_5ed57f9e (LSF_to_LPC): returns a[0..order] with a[0] = 1.

    Mirrors the engine's impulse-response evaluation of the P/Q polynomials in double precision.
    """
    lsf = np.array(lsf, np.float32)
    if np.any(np.diff(lsf) < 0):
        lsf = np.sort(lsf)  # FUN_5ed57f56 - TODO confirm it is a plain sort
    half = order // 2
    x = lsf.astype(np.float64)
    pc = np.zeros(half + 1)  # adStack_210 (odd-index LSFs -> P)
    qc = np.zeros(half + 1)  # adStack_2b0
    for i in range(half):
        pc[i + 1] = np.cos(np.float64(TWO_PI) * x[2 * i]) * -2.0
        qc[i + 1] = np.cos(np.float64(TWO_PI) * x[2 * i + 1]) * -2.0
    s1 = np.zeros(half + 1); s2 = np.zeros(half + 1)  # local_548 / local_350 (P delay lines)
    t1 = np.zeros(half + 1); t2 = np.zeros(half + 1)  # local_4a0 / local_3f8 (Q delay lines)
    a = np.zeros(order + 1, np.float32)
    prev = 0.0
    for n in range(order + 1):
        imp = 1.0 if n == 0 else 0.0
        p = np.zeros(half + 1); q = np.zeros(half + 1)
        p[0] = prev + imp
        q[0] = imp - prev
        prev = imp
        for j in range(half):
            p[j + 1] = pc[j + 1] * s2[j] + s1[j] + p[j]
            q[j + 1] = qc[j + 1] * t2[j] + t1[j] + q[j]
            s1[j] = s2[j]; s2[j] = p[j]
            t1[j] = t2[j]; t2[j] = q[j]
        if n:
            a[n - 1] = np.float32((q[half] + p[half]) * -0.5)
    # engine then shifts: out[k] = -out[k-1] ... and sets out[0] = 1  (see decompile tail)
    out = np.zeros(order + 1, np.float32)
    out[1:] = -a[:order]
    out[0] = 1.0
    return out


def ifft_split_radix(x, n, m, sine):
    """FUN_5ed58259 (IFFT_SplitRadix), literal port. x is modified in place (float32)."""
    x = x.astype(np.float32)
    r2 = np.float32(0.70710677)
    q = n // 2  # cosine offset into the sine table
    n2 = n * 2
    step = 1  # local_18
    for _ in range(m - 1):
        n2 //= 2
        n4 = n2 // 4
        n8 = n4 // 2
        is_, id_ = 0, n2 * 2
        while is_ < n - 1:
            for i in range(is_, n, id_):
                f1, f2 = x[i], x[i + 2 * n4]
                x[i] = f1 + f2
                x[i + n4] *= 2
                t = f1 - f2
                x[i + 2 * n4] = t - x[i + 3 * n4] * 2
                x[i + 3 * n4] = x[i + 3 * n4] * 2 + t
                if n4 > 1:
                    a = (x[i + n8 + n4] - x[i + n8]) * r2
                    b = (x[i + n8 + 2 * n4] + x[i + n8 + 3 * n4]) * r2
                    x[i + n8] = x[i + n8] + x[i + n8 + n4]
                    x[i + n8 + n4] = x[i + n8 + 3 * n4] - x[i + n8 + 2 * n4]
                    x[i + n8 + 2 * n4] = (b + a) * -2
                    x[i + n8 + 3 * n4] = (a - b) * 2
            is_ = id_ * 2 - n2
            id_ *= 4
        for j in range(1, n8):
            e = j * step
            cc1, ss1 = sine[2 * e + q], sine[2 * e]
            cc3, ss3 = sine[6 * e + q], sine[6 * e]
            is_, id_ = 0, n2 * 2
            while is_ < n - 1:
                for i in range(is_, n, id_):
                    i1 = i + j; i2 = i1 + n4; i3 = i2 + n4; i4 = i3 + n4
                    i5 = i + n4 - j; i6 = i5 + n4; i7 = i6 + n4; i8 = i7 + n4
                    x1, x2, x3, x4, x5, x6, x7, x8 = x[i1], x[i2], x[i3], x[i4], x[i5], x[i6], x[i7], x[i8]
                    x[i1] = x1 + x6
                    x[i5] = x5 + x2
                    x[i6] = x8 - x3
                    x[i2] = x4 - x7
                    t1 = (x1 - x6) - (x7 + x4)
                    t2 = x7 + x4 + (x1 - x6)
                    t3 = (x5 - x2) - (x8 + x3)
                    t4 = x8 + x3 + (x5 - x2)
                    x[i3] = t1 * cc1 + t3 * ss1
                    x[i7] = t1 * ss1 - t3 * cc1
                    x[i4] = cc3 * t2 - ss3 * t4
                    x[i8] = ss3 * t2 + cc3 * t4
                is_ = id_ * 2 - n2
                id_ *= 4
        step *= 2
    # length-two butterflies
    is_, id_ = 0, 4
    while is_ < n - 1:
        for i in range(is_, n, id_):
            a = x[i]
            x[i] = a + x[i + 1]
            x[i + 1] = a - x[i + 1]
        is_ = id_ * 2 - 2
        id_ *= 4
    # bit reversal
    j = 0
    for i in range(n - 1):
        if i < j:
            x[i], x[j] = x[j], x[i]
        k = n // 2
        while k <= j:
            j -= k
            k //= 2
        j += k
    x *= np.float32(1.0 / n)
    return x


def decode_unit(v, k):
    """FUN_5ed58ee3 (Voice_DecodeUnit). Returns (periods, lpc[nfr, order+1], gains, excitation)."""
    d = v.data
    p = v.base + int(v.unit_off[k])
    nfr = struct.unpack_from("<i", d, p)[0]
    periods, p = read_escaped(d, p + 4, nfr)
    lpc = np.zeros((nfr, v.order + 1), np.float32)
    for f in range(nfr):
        lsf = np.concatenate([v.cb_vec(cb, d[p + i]) for i, cb in enumerate(v.lsf_cb)])
        p += len(v.lsf_cb)
        lpc[f] = lsf_to_lpc(lsf, v.order)
    gains = np.frombuffer(d, "<f4", nfr, p).copy()
    p += 4 * nfr
    lens = np.abs(periods).astype(np.int32)
    exc = np.zeros(int(lens.sum()), np.float32)
    spec = np.zeros(v.fft_n, np.float32)
    pos = 0
    N = v.fft_n
    for f in range(nfr):
        n = int(lens[f])
        if periods[f] <= 0:  # unvoiced: scaled noise
            g = np.float32(gains[f] * np.float32(0.02))
            if v.noise_pos + n >= v.rate:
                v.noise_pos = 0
            exc[pos:pos + n] = g * v.noise[v.noise_pos:v.noise_pos + n]
            v.noise_pos += n
        else:
            delta = v.dexc_cb and f > 0 and periods[f - 1] >= 0
            if not delta:
                spec[:] = 0
            books = v.dexc_cb if delta else v.exc_cb
            kbin = 1
            for cb in books:
                vec = v.cb_vec(cb, d[p]); p += 1
                half = cb[0] // 2
                lo = slice(kbin, kbin + half)
                hi = slice(N - kbin - half + 1, N - kbin + 1)
                if delta:
                    spec[lo] += vec[:half]; spec[hi] += vec[half:]
                else:
                    spec[lo] = vec[:half]; spec[hi] = vec[half:]
                kbin += half
            buf = ifft_split_radix(spec.copy(), N, v.fft_log2, v.sine) * gains[f]
            m = min(n, N // 2)  # Pulse_CenterWrap
            out = np.zeros(n, np.float32)
            out[:m] = buf[:m]
            out[n - m:] += buf[N - m:]
            exc[pos:pos + n] = out
        pos += n
    return periods, lpc, gains, exc


def lpc_filter(exc, a, mem, gain=1.0):
    """FUN_5ed42dd7 (LPC_AllPoleFilter): y[n] = a0*x[n] - sum a[i]*y[n-i]; mem holds past outputs."""
    order = len(a) - 1
    y = np.empty(len(exc), np.float32)
    for n, x in enumerate(exc):
        acc = a[0] * x
        for i in range(order, 0, -1):
            acc -= mem[i] * a[i]
            mem[i] = mem[i - 1] if i > 1 else acc
        mem[0] = acc
        y[n] = gain * acc
    return y


def synth_units(v, units):
    """Play units back at their stored pitch and timing (no prosody modification)."""
    mem = np.zeros(v.order + 1, np.float32)
    out = []
    for k in units:
        periods, lpc, gains, exc = decode_unit(v, k)
        lens = np.abs(periods).astype(int)
        pos = 0
        for f, n in enumerate(lens):
            out.append(lpc_filter(exc[pos:pos + n], lpc[f], mem))
            pos += n
    return np.concatenate(out) if out else np.zeros(0, np.float32)


def write_wav(path, x, rate):
    pcm = np.clip(np.round(x), -32768, 32767).astype("<i2")
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(rate)
        w.writeframes(pcm.tobytes())


# ---------------------------------------------------------------------------
# Full back end: segment records -> PCM (Synth_RenderChunk / Speak_NextSentence)
# ---------------------------------------------------------------------------
_f32 = np.float32


def fast_ifft(x, n, m, sine):
    """numpy equivalent of ifft_split_radix (verified to ~1e-7)."""
    X = x[:n // 2 + 1].astype(np.complex128)
    X.imag[1:n // 2] = x[n - 1:n // 2:-1]
    return np.fft.irfft(X, n).astype(np.float32)


def fetch_period(src, src_n, dst_n, reverse, gain, window):
    """Excitation_FetchPeriod + FUN_5ed42afd."""
    if reverse:
        dst = src[:dst_n][::-1].copy()
    elif src_n == dst_n:
        dst = src[:dst_n].copy()
    else:
        dst = np.zeros(dst_n, _f32)
        m = min(src_n, dst_n)
        dst[0] = src[0]
        step = np.float64(len(window)) / m
        acc = 0.5
        for i in range(1, m):
            acc += step
            w = window[int(acc)]
            dst[i] = _f32(np.float64(w) * src[i] + dst[i])
            dst[dst_n - i] = _f32(np.float64(w) * src[src_n - i] + dst[dst_n - i])
    if gain != 1.0:
        dst = (dst.astype(np.float64) * np.float64(gain)).astype(_f32)
    return dst


def lpc_filter_exact(x, a, mem, gain):
    """LPC_AllPoleFilter with the engine's float32-per-step rounding."""
    order = len(a) - 1
    y = np.empty(len(x), _f32)
    a64 = a.astype(np.float64)
    for n, xv in enumerate(x):
        acc = _f32(a64[0] * xv)
        for i in range(order, 0, -1):
            acc = _f32(np.float64(acc) - np.float64(mem[i]) * a64[i])
            mem[i] = mem[i - 1] if i > 1 else acc
        mem[0] = acc
        y[n] = _f32(np.float64(gain) * acc)
    return y


def knot_interp(t, vals, n, x):
    i = 1
    while i < n - 1 and not (x <= t[i]):
        i += 1
    return (np.float64(vals[i]) - vals[i - 1]) * (x - np.float64(t[i - 1])) / (np.float64(t[i]) - t[i - 1]) + vals[i - 1]


def render_segments(v, segs, gain=1.0, exact_ifft=False):
    """segs: list of dicts from marks_model.seg_fields. Returns float samples (pre-PCM)."""
    import marks_model as mm
    global ifft_split_radix
    saved = ifft_split_radix
    if not exact_ifft:
        ifft_split_radix = fast_ifft
    try:
        mem = np.zeros(v.order + 1, _f32)
        out = []
        chunk_pos = 0
        for seg in segs:
            if seg["unit"] == 0:
                n = int(np.float64(_f32(v.rate)) * np.float64(_f32(seg["dur"])))
                mem[:] = 0
                out.append(np.zeros(n, _f32))
                chunk_pos = (chunk_pos + n) % 5000  # approximate chunk tracking for the 6000 guard
                continue
            periods, lpc, gains, exc = decode_unit(v, seg["unit"])
            lens = np.abs(periods).astype(np.int64)
            offs = np.concatenate([[0], np.cumsum(lens)])
            rate = mm.seg_rate(seg["dur"], int(lens.sum()), v.rate)
            frames, plen, flags = mm.place_marks(periods, seg, rate, float(v.rate))
            tsamp = 0
            for fr, P, fl in zip(frames, plen, flags):
                dst_n = int(P)
                if chunk_pos + dst_n > 6000:
                    dst_n = 999
                g = _f32(knot_interp(seg["t"], seg["amp"], seg["n"], tsamp))
                src = exc[offs[fr]:offs[fr + 1]]
                x = fetch_period(src, int(lens[fr]), dst_n, fl, g, v.window)
                a = lpc[fr].copy(); a[0] = 1.0
                out.append(lpc_filter_exact(x, a, mem, _f32(gain)))
                tsamp += dst_n
                chunk_pos += dst_n
                if chunk_pos > 4999:
                    chunk_pos = 0
        return np.concatenate(out) if out else np.zeros(0, _f32)
    finally:
        ifft_split_radix = saved


def to_pcm16(x):
    """FloatToPCM16: truncation toward zero (engine does not clip)."""
    return np.trunc(x).astype(np.int64).astype(np.int16)
