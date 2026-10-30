"""Parse samtap.exe logs (see harness/samtap.c for the record layouts)."""
import struct

import numpy as np

T_UNIT, T_MARKS, T_EXC, T_FILT, T_PCM, T_PFX, T_SENT = range(1, 8)


def records(path):
    d = open(path, "rb").read()
    o = 0
    while o + 8 <= len(d):
        tag, n = struct.unpack_from("<II", d, o)
        yield tag, d[o + 8:o + 8 + n]
        o += 8 + n


def f32(b, o, n):
    return np.frombuffer(b, "<f4", n, o).copy(), o + 4 * n


def parse(path):
    out = {"units": [], "marks": [], "exc": [], "filt": [], "pcm": [], "sent": []}
    for tag, b in records(path):
        if tag == T_UNIT:
            unit, nfr, total, order = struct.unpack_from("<4i", b)
            o = 16
            periods, o = f32(b, o, nfr)
            lpc, o = f32(b, o, nfr * (order + 1))
            gains, o = f32(b, o, nfr)
            exc, o = f32(b, o, total)
            out["units"].append(dict(unit=unit, periods=periods, lpc=lpc.reshape(nfr, order + 1),
                                     gains=gains, exc=exc))
        elif tag == T_MARKS:
            (n,) = struct.unpack_from("<i", b)
            frame = np.frombuffer(b, "<i4", n, 4).copy()
            period = np.frombuffer(b, "<f4", n, 4 + 4 * n).copy()
            flag = np.frombuffer(b, "<i2", n, 4 + 8 * n).copy()
            out["marks"].append(dict(frame=frame, period=period, flag=flag))
        elif tag == T_EXC:
            epoch, flag, srcn, dstn = struct.unpack_from("<4i", b)
            (gain,) = struct.unpack_from("<f", b, 16)
            dst, _ = f32(b, 20, dstn)
            out["exc"].append(dict(epoch=epoch, flag=flag, srcn=srcn, dstn=dstn, gain=gain, dst=dst))
        elif tag == T_FILT:
            n, gain, order = struct.unpack_from("<ifi", b)
            o = 12
            a, o = f32(b, o, order + 1)
            xin, o = f32(b, o, n)
            y, o = f32(b, o, n)
            out["filt"].append(dict(n=n, gain=gain, a=a, xin=xin, y=y))
        elif tag == T_PCM:
            n, stereo = struct.unpack_from("<2i", b)
            x, _ = f32(b, 8, n)
            out["pcm"].append(dict(stereo=stereo, x=x))
        elif tag == T_SENT:
            out["sent"].append(b)
    return out
