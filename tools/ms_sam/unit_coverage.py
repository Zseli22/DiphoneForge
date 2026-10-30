"""Report how much of Sam's unit table a corpus of phone instances can fill.

    python unit_coverage.py --samleaf build/ms_sam/samleaf.bin --textgrids output/mfa_aligned

The builder tries all four word positions for every instance, so this counts a unit as covered when any
position matches.  Units that are not covered still speak: spdbuild_sam.py fills them from the same
phone's most typical recording, which keeps the phoneme right and loses only the context detail.
"""
import argparse
import collections
import glob
import os

import numpy as np

from textgrid import read

VOW = "AA AE AH AO AW AY EH ER EY IH IY OW OY UH UW".split()


def load_table(path):
    d = open(path, "rb").read()
    assert d[:4] == b"SLF1"
    nph, nunits = np.frombuffer(d[4:12], "<i4")
    tab = np.frombuffer(d[12:12 + nph ** 3 * 16], "<i4").reshape(nph, nph, nph, 4)
    names = [x.decode() for x in d[12 + nph ** 3 * 16 + nph * 4:].split(b"\0")[:nph]]
    return tab, names, int(nunits)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--samleaf", required=True)
    ap.add_argument("--textgrids", default=os.path.join("output", "mfa_aligned"))
    args = ap.parse_args()

    tab, names, nunits = load_table(args.samleaf)
    pid = {n: i for i, n in enumerate(names)}
    sil = pid["SIL"]
    covered = set()
    inst = collections.Counter()
    unlabelled = collections.Counter()
    for f in sorted(glob.glob(os.path.join(args.textgrids, "*.TextGrid"))):
        phones = [(a, b, s.strip()) for a, b, s in read(f).get("phones", []) if s.strip()]
        for i, (a, b, s) in enumerate(phones):
            inst[s] += 1
            if s not in pid:
                unlabelled[s] += 1
                continue
            left = pid[phones[i - 1][2]] if i > 0 and phones[i - 1][2] in pid else sil
            right = pid[phones[i + 1][2]] if i + 1 < len(phones) and phones[i + 1][2] in pid else sil
            for q in range(4):
                u = int(tab[pid[s], left, right, q])
                if u >= 0:
                    covered.add(u)
    # an unstressed vowel is filled from its stressed recording
    for v in VOW:
        if v + "0" in pid:
            for f in sorted(glob.glob(os.path.join(args.textgrids, "*.TextGrid"))):
                phones = [(a, b, s.strip()) for a, b, s in read(f).get("phones", []) if s.strip()]
                for i, (a, b, s) in enumerate(phones):
                    if s != v + "1":
                        continue
                    left = pid[phones[i - 1][2]] if i > 0 and phones[i - 1][2] in pid else sil
                    right = pid[phones[i + 1][2]] if i + 1 < len(phones) and phones[i + 1][2] in pid else sil
                    for q in range(4):
                        u = int(tab[pid[v + "0"], left, right, q])
                        if u >= 0:
                            covered.add(u)
    print("phone instances       %d" % sum(inst.values()))
    print("distinct phone labels %d  (Sam has %d names)" % (len(inst), len(names)))
    if unlabelled:
        print("labels not in Sam's set:", dict(unlabelled))
    print("units covered         %d / %d  (%.1f%%)" % (len(covered), nunits, 100 * len(covered) / nunits))
    print("units not covered by a matching recording %d" % (nunits - len(covered)))
    print("\ninstances per phone")
    for p, n in sorted(inst.items(), key=lambda kv: -kv[1]):
        print("  %-6s %4d" % (p, n))


if __name__ == "__main__":
    main()
