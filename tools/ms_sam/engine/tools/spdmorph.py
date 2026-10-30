"""Make a new voice out of an existing one by resizing its head.

A .spd voice stores its vocal tract as line spectral frequencies (normalized 0..0.5) in split-VQ
codebooks, so moving those frequencies moves every formant - which is what a longer or shorter vocal
tract does. The move has to be a BILINEAR WARP, not a multiply: a multiply pushes the top formants
into the Nyquist frequency and the voice turns tinny and whistly. The warp maps 0 to 0 and 0.5 to
0.5, so the ends stay put and only the spacing between formants changes.

    w' = w + 2*atan(a*sin w / (1 - a*cos w))        w = 2*pi*lsf

a > 0 moves formants down (a bigger head), a < 0 up (a smaller one); useful values are about +-0.12.
The base pitch is separate - the int at 0x318 in the .sdf - so head size and pitch move independently.

  python tools/spdmorph.py Mike.spd Brutus.spd --warp 0.08 --pitch 95
  python tools/spdmorph.py Mike.spd Freddy.spd --warp -0.08 --pitch 130

The output is a voice file for the same engine, so any host that loads the original loads this one.
It is derived from Microsoft's voice data: keep it to your own machines.
"""
import argparse
import math
import os
import shutil
import struct
import sys


def u32(b, o):
    return struct.unpack_from("<I", b, o)[0]


def warp_lsf(v, alpha):
    """bilinear (all-pass) warp of one normalized frequency, 0 and 0.5 held fixed"""
    w = 2.0 * math.pi * v
    num = alpha * math.sin(w)
    den = 1.0 - alpha * math.cos(w)
    return (w + 2.0 * math.atan2(num, den)) / (2.0 * math.pi)


def morph(src, dst, alpha, verbose=True):
    b = bytearray(open(src, "rb").read())
    if bytes(b[:4]) != b"Data":
        sys.exit("%s is not a .spd voice file" % src)
    off3, sz3 = struct.unpack_from("<II", b, 0x18 + 24)
    s = off3
    nlsf = u32(b, s + 4)
    rate, order = u32(b, s), u32(b, s + 0x498)
    if verbose:
        print("%s: %d Hz, LPC order %d, %d LSF codebooks" % (os.path.basename(src), rate, order, nlsf))
    total = clamped = 0
    for i in range(nlsf):
        dim = u32(b, s + 0x14 + 12 * i)
        off = u32(b, s + 0x18 + 12 * i)
        cnt = u32(b, s + 0x1C + 12 * i)
        base = s + off
        for e in range(cnt * dim):
            p = base + 4 * e
            v = struct.unpack_from("<f", b, p)[0]
            w = warp_lsf(v, alpha)
            if w > 0.4995:                      # stay inside the Nyquist the engine assumes
                w, clamped = 0.4995, clamped + 1
            elif w < 0.0005:
                w, clamped = 0.0005, clamped + 1
            struct.pack_into("<f", b, p, w)
            total += 1
        if verbose:
            print("  book %d: %d entries x %d" % (i, cnt, dim))
    open(dst, "wb").write(bytes(b))
    if verbose:
        print("warped %d line spectral frequencies with a = %+.3f (%d clamped)" % (total, alpha, clamped))


def set_pitch(src_sdf, dst_sdf, hz):
    """the voice's base pitch: 'Vois' header, int at 0x318 (Sam 100, Mike 110, Mary 189)"""
    b = bytearray(open(src_sdf, "rb").read())
    if bytes(b[:4]) != b"Vois" or len(b) < 0x31C:
        return False
    old = struct.unpack_from("<i", b, 0x318)[0]
    struct.pack_into("<i", b, 0x318, int(hz))
    open(dst_sdf, "wb").write(bytes(b))
    print("base pitch %d Hz -> %d Hz" % (old, int(hz)))
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--warp", type=float, default=0.0,
                    help="bilinear warp: >0 a bigger head (formants down), <0 a smaller one; try +-0.04 to 0.12")
    ap.add_argument("--pitch", type=float, default=0, help="base pitch in Hz (0 = keep the original's)")
    a = ap.parse_args()

    morph(a.src, a.dst, a.warp)
    src_sdf = os.path.splitext(a.src)[0] + ".sdf"
    dst_sdf = os.path.splitext(a.dst)[0] + ".sdf"
    if not os.path.exists(src_sdf):
        for ext in (".SDF", ".Sdf"):
            if os.path.exists(os.path.splitext(a.src)[0] + ext):
                src_sdf = os.path.splitext(a.src)[0] + ext
                break
    if os.path.exists(src_sdf):
        if a.pitch > 0:
            set_pitch(src_sdf, dst_sdf, a.pitch)
        else:
            shutil.copy2(src_sdf, dst_sdf)
    print("wrote %s" % a.dst)


if __name__ == "__main__":
    main()
