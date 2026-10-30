"""Compare prosody_model against a samtap log (tags 9/10)."""
import struct
import sys

import numpy as np

import prosody_model as pm
import taplog

IT = 0x12C


def check(path, verbose=True):
    sentences = []
    cur = None
    for tag, b in taplog.records(path):
        if tag == 9:
            base, rng, n = struct.unpack_from("<ffi", b)
            cur = dict(base=base, rng=rng, items=[pm.item_from_bytes(b[12 + i * IT:12 + (i + 1) * IT]) for i in range(n)])
        elif tag == 10 and cur:
            (n2,) = struct.unpack_from("<i", b)
            cur["out"] = [b[4 + i * IT:4 + (i + 1) * IT] for i in range(n2)]
            o = 4 + n2 * IT
            (L,) = struct.unpack_from("<i", b, o)
            cur["stype"] = struct.unpack_from("<16i", b, o + 4 + 4 * L)[3]
            sentences.append(cur)
            cur = None
    tot = ok = 0
    worst = 0.0
    for s in sentences:
        _, res = pm.build(s["items"], s["base"], s["rng"], s["stype"])
        for k, (r, ob) in enumerate(zip(res, s["out"])):
            f0r = np.frombuffer(ob, "<f4", 20, 0x64)
            tr = np.frombuffer(ob, "<f4", 20, 0x14)
            e8, ec = struct.unpack_from("<ff", ob, 0xE8)
            f0o = np.array(r["f0"], np.float32)
            same = np.array_equal(f0o, f0r) and np.array_equal(np.array(r["t"], np.float32), tr) and \
                r["top"] == np.float32(e8) and r["bot"] == np.float32(ec)
            tot += 1
            ok += same
            worst = max(worst, float(np.abs(f0o - f0r).max()))
            if not same and verbose:
                print(f"  item {k} type {s['items'][k]['type']} accent {s['items'][k]['accent']} bound {s['items'][k]['bound']}: "
                      f"f0 maxdiff {np.abs(f0o - f0r).max():.4g} top {r['top']}/{e8} bot {r['bot']}/{ec}")
    print(f"{path}: {len(sentences)} sentences, {ok}/{tot} items exact, worst f0 diff {worst:.4g} Hz")
    return ok, tot


if __name__ == "__main__":
    for p in sys.argv[1:]:
        check(p)
