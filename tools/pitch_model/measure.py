#!/usr/bin/env python3
"""Say what a wave file's pitch actually did, frame by frame.

An autocorrelation tracker, good enough to check that a contour went where it
was meant to and that two renders of the same durations differ only in pitch.
It is a measurement, not a model: nothing else in this folder uses it.
"""

import argparse
import sys
import wave

import numpy as np


def read_wav(path):
    with wave.open(path, "rb") as w:
        rate = w.getframerate()
        channels = w.getnchannels()
        frames = w.readframes(w.getnframes())
    data = np.frombuffer(frames, dtype=np.int16).astype(np.float64)
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return data, rate


def track(path, win_ms=40, hop_ms=10, lo=55, hi=250):
    """F0 by autocorrelation, taking the first strong peak rather than the
    highest, which is what stops an octave error on a clean vowel."""
    data, rate = read_wav(path)
    win = int(rate * win_ms / 1000)
    hop = int(rate * hop_ms / 1000)
    window = np.hamming(win)
    out = []
    for start in range(0, max(0, len(data) - win), hop):
        frame = data[start:start + win]
        frame = frame - frame.mean()
        energy = float(np.sqrt((frame ** 2).mean()))
        if energy < 300:
            continue
        corr = np.correlate(frame * window, frame * window, mode="full")
        corr = corr[win - 1:]
        if corr[0] <= 0:
            continue
        lo_lag = max(2, int(rate / hi))
        hi_lag = min(len(corr) - 2, int(rate / lo))
        if hi_lag <= lo_lag + 2:
            continue
        seg = corr[lo_lag:hi_lag + 1]
        peak = float(seg.max())
        if peak <= 0:
            continue
        best = None
        for i in range(1, len(seg) - 1):
            if (seg[i] >= seg[i - 1] and seg[i] >= seg[i + 1]
                    and seg[i] >= 0.75 * peak and seg[i] / corr[0] >= 0.45):
                best = lo_lag + i
                break
        if best is None:
            best = lo_lag + int(np.argmax(seg))
        out.append((start / rate, rate / best))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("wav")
    ap.add_argument("--every", type=float, default=50.0,
                    help="print a point every N ms")
    args = ap.parse_args(argv)

    pts = track(args.wav)
    if not pts:
        sys.exit("no voiced frames")
    vals = [f for _t, f in pts]
    print("%s: %d voiced frames, %.1f-%.1f Hz, median %.1f"
          % (args.wav, len(pts), min(vals), max(vals),
             float(np.median(vals))))
    t = 0.0
    for when, f in pts:
        if when >= t:
            print("  %6.0f ms  %6.1f Hz" % (when * 1000, f))
            t = when + args.every / 1000.0
    return 0


if __name__ == "__main__":
    sys.exit(main())
