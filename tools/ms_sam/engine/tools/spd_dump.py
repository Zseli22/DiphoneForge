"""Dump the structure of a SAPI5 Microsoft TTS voice file (Sam.spd / Mike.SPD / Mary.SPD).

Layout recovered from spttseng.dll (FUN_5ed58d7a = voice load, FUN_5ed58ee3 = Voice_DecodeUnit).
Offsets inside the synth section ("sec3") are named after how the engine uses them.
"""
import struct
import sys

SECTION_NAMES = {0: "sec0", 1: "sec1", 2: "sec2", 3: "synth", 4: "sec4 (8-byte records)"}


def u32(d, o): return struct.unpack_from("<I", d, o)[0]
def i32(d, o): return struct.unpack_from("<i", d, o)[0]
def f32(d, o): return struct.unpack_from("<f", d, o)[0]


def read_escaped(d, o, n):
    """Signed bytes; 0x7F / 0x80 mean 'add the next byte too' (FUN_5ed57ea2)."""
    out = []
    for _ in range(n):
        c = struct.unpack_from("b", d, o)[0]
        if c in (127, -128):
            v = float(c)
            while struct.unpack_from("b", d, o)[0] == c:
                o += 1
                v += struct.unpack_from("b", d, o)[0]
            out.append(v)
        else:
            out.append(float(c))
        o += 1
    return out, o


def main(path):
    d = open(path, "rb").read()
    assert d[:4] == b"Data" and u32(d, 4) == 0x10000, "not a v1 .spd"
    print(f"{path}: {len(d)} bytes, guid {d[8:24].hex()}")
    secs = [(u32(d, 0x18 + i * 8), u32(d, 0x1C + i * 8)) for i in range(5)]
    for i, (off, size) in enumerate(secs):
        print(f"  section {i} {SECTION_NAMES[i]:<22} off {off:>8} size {size:>8}")

    s = secs[3][0]  # synth section base
    H = lambda o: i32(d, s + o)
    n_lsf_cb, n_exc_cb, n_dexc_cb = H(4), H(8), H(0xC)
    order, fft_n, fft_log2 = H(0x498), H(0x49C), H(0x4A0)
    print(f"\nsynth header: word0 {H(0)} (as float {f32(d, s):.1f})")
    print(f"  LSF codebooks {n_lsf_cb}, excitation codebooks {n_exc_cb}, delta-excitation codebooks {n_dexc_cb}")
    print(f"  LPC order {order}, FFT size {fft_n} (2^{fft_log2}), +0x490 = {H(0x490)}")
    for name, base, n in (("LSF", 0x14, n_lsf_cb), ("exc", 0x194, n_exc_cb), ("dexc", 0x314, n_dexc_cb)):
        rows = [(H(base + i * 12), H(base + 4 + i * 12), H(base + 8 + i * 12)) for i in range(n)]
        print(f"  {name} codebooks (dim, data_off, ?): {rows}")
    unit_tab, tw, t2, noise = H(0x494), H(0x4A4), H(0x4A8), H(0x4AC)
    print(f"  unit offset table @+{unit_tab}, twiddles @+{tw}, +0x4a8 @+{t2}, noise @+{noise}")

    n_units = H(0x490)
    units = [i32(d, s + unit_tab + k * 4) for k in range(n_units)]
    bad = [k for k, uo in enumerate(units) if not 0 < uo < secs[3][1]]
    frames = [i32(d, s + uo) for uo in units]
    print(f"\nunits: {n_units}, offsets out of range: {len(bad)}, "
          f"frames/unit min {min(frames)} max {max(frames)} total {sum(frames)}")
    periods = []
    for k in (0, 1, n_units // 2, n_units - 1):
        p = s + units[k]
        vals, _ = read_escaped(d, p + 4, frames[k])
        print(f"  unit {k:>5} @+{units[k]}: {frames[k]} frames, "
              f"{sum(v > 0 for v in vals)} voiced, values {vals[:10]}")
    for k in range(n_units):
        vals, _ = read_escaped(d, s + units[k] + 4, frames[k])
        periods += [v for v in vals if v > 0]
    periods.sort()
    rate = H(0)
    med = periods[len(periods) // 2]
    print(f"  voiced period median {med:.0f} samples = {rate / med:.1f} Hz "
          f"(range {rate / periods[-1]:.0f}-{rate / periods[0]:.0f} Hz)")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else
         r"C:\Program Files (x86)\Common Files\SpeechEngines\Microsoft\TTS\1033\sam.spd")
