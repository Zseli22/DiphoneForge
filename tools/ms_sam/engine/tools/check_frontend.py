"""Check frontend_model (word records -> segments) against a samtap log."""
import struct
import sys

import numpy as np

import frontend_model as fm
import marks_model as mm
import samvoc as s
import taplog
import units_model as um

V = s.Voice()
SEL = um.UnitSelector(V.data, V.sections)
_o4 = V.sections[4][0]
_cnt = V.sections[4][1] // 8


def vmap(ph, st):
    return struct.unpack_from("<h", V.data, _o4 + (ph * 2 + (_cnt * 2 if st else 0)) * 2)[0]


def sentences(path):
    """Group log records per sentence (word records ... prosody in/out ... segments)."""
    out, cur = [], None
    for tag, b in taplog.records(path):
        if tag == 11:
            if cur is None or cur.get("done"):
                cur = dict(words=[], segs=[], items=None, stype=1)
                out.append(cur)
            cur["words"].append(fm.word_from_bytes(b[8:]))
        elif tag == 9 and cur:
            cur["items"] = b
        elif tag == 10 and cur:
            (n2,) = struct.unpack_from("<i", b)
            o = 4 + n2 * 0x12C
            (L,) = struct.unpack_from("<i", b, o)
            cur["stype"] = struct.unpack_from("<16i", b, o + 4 + 4 * L)[3]
            cur["done"] = True
        elif tag == 7:
            # segments belong to the most recent finished sentence
            for c in reversed(out):
                if c.get("done"):
                    c["segs"].append(mm.seg_fields(b))
                    break
    return out


def check(path, verbose=True):
    tot = ok = 0
    for si, sent in enumerate(sentences(path)):
        items = fm.build_items(sent["words"])
        fm.position_flags(items)
        fm.select_units(items, SEL, vmap)
        fm.durations(items)
        segs = fm.segments(items, stype=sent["stype"])
        real = sent["segs"]
        for k, (a, b) in enumerate(zip(segs, real)):
            same = a["unit"] == b["unit"] and np.float32(a["dur"]) == np.float32(b["dur"]) and \
                np.array_equal(a["t"], b["t"]) and np.array_equal(a["f0"], b["f0"]) and np.array_equal(a["amp"], b["amp"])
            tot += 1
            ok += same
            if not same and verbose:
                print(f"  s{si} seg {k}: unit {a['unit']}/{b['unit']} dur {a['dur']:.5f}/{b['dur']:.5f} "
                      f"t {np.abs(a['t'] - b['t']).max():.3g} f0 {np.abs(a['f0'] - b['f0']).max():.3g} amp {np.abs(a['amp'] - b['amp']).max():.3g}")
        if len(segs) != len(real):
            print(f"  s{si}: {len(segs)} segments vs {len(real)} real")
    print(f"{path}: {ok}/{tot} segments exact")
    return ok, tot


if __name__ == "__main__":
    for p in sys.argv[1:]:
        check(p)
