"""Check word_prosody passes against samtap tag-12 dumps (pass 0..3 word lists)."""
import struct
import sys

import frontend_model as fm
import taplog
import word_prosody as wp

WD = 0x698
KEYS = ["text", "btype", "acc", "prom", "bound", "bstr", "emph", "silence_ms", "rule_a", "rule_b", "x694"]


def load(path):
    sents = []
    cur = {}
    for tag, b in taplog.records(path):
        if tag == 12:
            p, n = struct.unpack_from("<ii", b)
            if p == 0 and cur:
                sents.append(cur)
                cur = {}
            cur[p] = [fm.word_from_bytes(b[8 + k * WD: 8 + (k + 1) * WD]) for k in range(n)]
    if cur:
        sents.append(cur)
    return sents


def diff(ours, real, label):
    bad = 0
    if len(ours) != len(real):
        print(f"   {label}: {len(ours)} words vs {len(real)}: ours {[w['text'] for w in ours]} real {[w['text'] for w in real]}")
        return 1
    for a, b in zip(ours, real):
        d = {k: (a[k], b[k]) for k in KEYS if a[k] != b[k]}
        if d:
            bad += 1
            print(f"   {label} {b['text']!r}: {d}")
    return bad


def check(path):
    rand = wp.MsvcRand(1)
    tot = 0
    for s in load(path):
        import copy
        w0 = copy.deepcopy(s[0])
        w1 = wp.pass0(copy.deepcopy(w0))
        tot += diff(w1, s[1], "pass0")
        w2 = wp.pass1(copy.deepcopy(s[1]))
        tot += diff(w2, s[2], "pass1")
        w3 = wp.pass2(copy.deepcopy(s[2]), rand)
        tot += diff(w3, s[3], "pass2")
    print(f"{path}: {'OK' if tot == 0 else str(tot) + ' mismatches'}")


if __name__ == "__main__":
    for p in sys.argv[1:]:
        check(p)
