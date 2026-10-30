"""Band-by-band long-term spectrum of several renders, to see where a voice's timbre goes wrong.

usage: python bandcmp.py a.wav b.wav ...
"""
import sys

import numpy as np

from voicecheck import ltas, read

BANDS = [(0, 300), (300, 800), (800, 1500), (1500, 2500), (2500, 4000), (4000, 6000), (6000, 8000)]


def main():
    files = sys.argv[1:]
    names = [f.replace("\\", "/").split("/")[-1].replace(".wav", "") for f in files]
    L = [ltas(read(f)) for f in files]
    f = np.fft.rfftfreq(512, 1.0 / 16000)
    print("band Hz      " + "  ".join("%8s" % n for n in names))
    for a, b in BANDS:
        m = (f >= a) & (f < b)
        print("%5d-%5d  " % (a, b) + "  ".join("%8.1f" % x[m].mean() for x in L))


if __name__ == "__main__":
    main()
