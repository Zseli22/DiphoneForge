"""Speak every line of a text file with the real engine (samtap, no hooks) and the C build; compare audio.

usage: python tools/bulk_compare.py sentences.txt [outdir]
Real renders are cached in outdir (default: next to the text file, <name>_ref/).
"""
import os
import subprocess
import sys
import wave

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(ROOT, "build", "x64", "sam_say.exe")
TAP = os.path.join(ROOT, "harness", "samtap.exe")


def rd(p):
    w = wave.open(p)
    return np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(int)


def main():
    src = sys.argv[1]
    lines = [l.strip() for l in open(src, encoding="utf-8") if l.strip()]
    out = sys.argv[2] if len(sys.argv) > 2 else os.path.splitext(src)[0] + "_ref"
    os.makedirs(out, exist_ok=True)
    good = 0
    bad = []
    for i, text in enumerate(lines):
        real = os.path.join(out, f"{i:04d}.wav")
        mine = os.path.join(out, f"c_{i:04d}.wav")
        txt = os.path.join(out, f"{i:04d}.txt")
        if not os.path.exists(real) or not os.path.exists(txt) or open(txt, encoding="utf-8").read() != text:
            open(txt, "w", encoding="utf-8").write(text)
            subprocess.run([TAP, "@" + txt, real, os.path.join(out, "tmp.log"), "0"], capture_output=True)
        subprocess.run([EXE, "--data", os.path.join(ROOT, "data", "voice"), "@" + txt, mine], capture_output=True)
        a, b = rd(mine), rd(real)
        n = min(len(a), len(b))
        md = int(np.abs(a[:n] - b[:n]).max()) if n else -1
        ok = len(a) == len(b) and md <= 1
        good += ok
        if not ok:
            bad.append(i)
            print(f"{i:4} len C {len(a):7} real {len(b):7} maxdiff {md:6}  {text[:70]}")
    print(f"{good}/{len(lines)} match; differing: {bad}")


if __name__ == "__main__":
    main()
