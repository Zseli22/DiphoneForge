#!/usr/bin/env python3
"""Measure what a reference recording's pitch actually did.

Reads one wave file, tracks F0, and reports the things the model has to
reproduce: where the accents are, how high they go, what the level does
between them, how far the sentence declines, and where it lands.  Levels are
semitones about the utterance's own median, so the pitch slider, the voice
and the sample rate all drop out.

    python tools/pitch_model/analyse_ref.py recording.wav --plot
"""

import argparse
import math
import sys
import wave

import numpy as np


def read_wav(path):
    with wave.open(path, "rb") as w:
        rate = w.getframerate()
        channels = w.getnchannels()
        raw = w.readframes(w.getnframes())
    data = np.frombuffer(raw, dtype=np.int16).astype(np.float64)
    if channels > 1:
        data = data.reshape(-1, channels).mean(axis=1)
    return data, rate


def track(data, rate, hop_ms=5.0, win_ms=40.0, lo=50.0, hi=320.0):
    """F0 by normalised autocorrelation, first strong peak, with a voicing
    test.  A synthetic voice is very periodic, so this is reliable on it."""
    win = int(rate * win_ms / 1000)
    hop = max(1, int(rate * hop_ms / 1000))
    window = np.hanning(win)
    lo_lag = max(2, int(rate / hi))
    hi_lag = min(win - 2, int(rate / lo))
    out = []
    for start in range(0, max(0, len(data) - win), hop):
        frame = data[start:start + win]
        frame = (frame - frame.mean()) * window
        energy = float(np.sqrt((frame ** 2).mean()))
        if energy < 200:
            out.append((start / rate, None))
            continue
        corr = np.correlate(frame, frame, mode="full")[win - 1:]
        if corr[0] <= 0:
            out.append((start / rate, None))
            continue
        norm = corr / corr[0]
        seg = norm[lo_lag:hi_lag + 1]
        if not len(seg):
            out.append((start / rate, None))
            continue
        top = float(seg.max())
        if top < 0.4:
            out.append((start / rate, None))
            continue
        best = None
        for i in range(1, len(seg) - 1):
            if seg[i] >= seg[i - 1] and seg[i] >= seg[i + 1] and \
                    seg[i] >= 0.85 * top:
                best = lo_lag + i
                break
        if best is None:
            best = lo_lag + int(np.argmax(seg))

        # An octave correction: if twice the lag is nearly as periodic as the
        # one chosen, the true period is the longer one and the short one is
        # a half-period of it.  Reading a sub-period is the commonest way a
        # tracker puts a voice an octave too high.
        for mult in (2, 3):
            longer = best * mult
            if longer <= hi_lag and norm[longer] >= 0.85 * norm[best]:
                best = longer

        out.append((start / rate, rate / best))
    return out


def smooth(points, half=2):
    """A short median filter, because a synthetic voice still has the odd
    frame where the tracker takes the wrong period."""
    vals = [f for _t, f in points]
    out = []
    for i, (t, f) in enumerate(points):
        if f is None:
            out.append((t, None))
            continue
        near = [v for v in vals[max(0, i - half):i + half + 1] if v]
        out.append((t, float(np.median(near)) if near else None))
    return out


def despike(points, limit=4.0, half=4):
    """A second pass, for spikes that survived.

    A frame with no voiced neighbour at all is a stray and goes.  A frame at
    the edge of a real run is kept, because that edge is the bottom of a
    final fall and losing it loses the landing.
    """
    out = []
    for i, (t, s) in enumerate(points):
        if s is None:
            out.append((t, None))
            continue
        first = max(0, i - half)
        near = [v for j, (_tt, v) in enumerate(points[first:i + half + 1])
                if v is not None and (first + j) != i]
        if not near:
            out.append((t, None))
            continue
        med = float(np.median(near))
        out.append((t, None if abs(s - med) > limit else s))
    return out


def accent_shape(points, peak_t, peak_s):
    """How fast an accent rises and falls, and how wide it is.

    Walk out either side to the first point that falls `1/e` of the
    prominence below the peak, which is a fair way to compare a rounded
    accent with a corner.
    """
    seq = [(t, s) for t, s in points if s is not None]
    idx = min(range(len(seq)), key=lambda i: abs(seq[i][0] - peak_t))

    # the minimum before and after, and the time to reach it
    left_t, left_low = peak_t, peak_s
    j = idx
    while j > 0 and seq[j - 1][1] <= seq[j][1]:
        j -= 1
        if seq[j][1] < left_low:
            left_low, left_t = seq[j][1], seq[j][0]
    right_t, right_low = peak_t, peak_s
    j = idx
    while j < len(seq) - 1 and seq[j + 1][1] <= seq[j][1]:
        j += 1
        if seq[j][1] < right_low:
            right_low, right_t = seq[j][1], seq[j][0]

    # half-prominence width, using the deeper of the two minima
    floor = min(left_low, right_low)
    half = peak_s - 0.5 * (peak_s - floor)
    j = idx
    while j > 0 and seq[j - 1][1] > half:
        j -= 1
    half_left = seq[j][0]
    j = idx
    while j < len(seq) - 1 and seq[j + 1][1] > half:
        j += 1
    half_right = seq[j][0]

    return {
        "rise_ms": 1000.0 * abs(peak_t - left_t),
        "fall_ms": 1000.0 * abs(right_t - peak_t),
        "width_ms": 1000.0 * (half_right - half_left),
        "prominence": peak_s - floor,
    }


def median_of(points):
    vals = [f for _t, f in points if f]
    return float(np.median(vals)) if vals else 0.0


def to_semitones(points, base):
    return [(t, None if f is None else 12.0 * math.log2(f / base))
            for t, f in points]


def reject_outliers(points, limit=5.0, half=11):
    """Drop frames that jumped an octave or caught a stray period.

    A synth is very periodic, but at a voicing onset, or on a stop burst, the
    tracker can take the wrong period.  A frame more than `limit` semitones
    from the median of its neighbours is not the pitch of this voice.
    """
    vals = [s for _t, s in points]
    out = []
    for i, (t, s) in enumerate(points):
        if s is None:
            out.append((t, None))
            continue
        near = [v for v in vals[max(0, i - half):i + half + 1]
                if v is not None]
        if len(near) < 3:
            out.append((t, s))
            continue
        med = float(np.median(near))
        out.append((t, None if abs(s - med) > limit else s))
    return out


def runs(points, gap=0.06):
    """Contiguous voiced stretches, long enough to be a syllable."""
    out, cur = [], []
    last_t = None
    for t, s in points:
        if s is None:
            if cur and last_t is not None and t - last_t > gap:
                out.append(cur)
                cur = []
            continue
        cur.append((t, s))
        last_t = t
    if cur:
        out.append(cur)
    return [r for r in out if r[-1][0] - r[0][0] >= 0.03]


def peaks(points, min_prominence=1.0, min_sep=0.06):
    """Local maxima of the smoothed contour, with a prominence test."""
    seq = [(t, s) for t, s in points if s is not None]
    found = []
    for i in range(1, len(seq) - 1):
        t, s = seq[i]
        if s < seq[i - 1][1] or s < seq[i + 1][1]:
            continue
        # prominence: how far the contour fell either side before rising
        left = s
        j = i - 1
        while j >= 0 and seq[j][1] <= left + 1e-9:
            left = min(left, seq[j][1])
            j -= 1
        right = s
        j = i + 1
        while j < len(seq) and seq[j][1] <= right + 1e-9:
            right = min(right, seq[j][1])
            j += 1
        prom = s - max(left, right)
        if prom >= min_prominence:
            if found and t - found[-1][0] < min_sep:
                if s > found[-1][1]:
                    found[-1] = (t, s)
                continue
            found.append((t, s))
    return found


def plot(points, width=72, height=13, lo=None, hi=None):
    seq = [(t, s) for t, s in points if s is not None]
    if not seq:
        return ""
    total = points[-1][0] or 1.0
    vals = [s for _t, s in seq]
    lo = math.floor(min(vals)) if lo is None else lo
    hi = math.ceil(max(vals)) if hi is None else hi
    if hi - lo < 1:
        hi = lo + 1
    grid = [[" "] * width for _ in range(height)]
    for t, s in points:
        if s is None:
            continue
        x = min(width - 1, int(round((width - 1) * t / total)))
        y = int(round((height - 1) * (s - lo) / (hi - lo)))
        y = min(height - 1, max(0, y))
        grid[y][x] = "#"
    rows = []
    for r, row in enumerate(reversed(grid)):
        val = hi - (hi - lo) * r / (height - 1)
        rows.append("%+5.1f |%s" % (val, "".join(row)))
    rows.append("      +" + "-" * width)
    rows.append("       0" + " " * (width - 14) + "%.2f s" % total)
    return "\n".join(rows)


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("wav")
    ap.add_argument("--plot", action="store_true")
    ap.add_argument("--accents", action="store_true",
                    help="rise, fall and width of each accent")
    ap.add_argument("--flat", action="store_true",
                    help="how much of the contour sits still")
    ap.add_argument("--words", type=int, default=0,
                    help="words in the sentence, for a rate figure")
    args = ap.parse_args(argv)

    data, rate = read_wav(args.wav)
    raw = track(data, rate)
    base0 = median_of(raw)
    pts = reject_outliers(to_semitones(raw, base0))
    vals = [s for _t, s in pts if s is not None]
    if not vals:
        sys.exit("no voiced frames")
    shift = float(np.median(vals))
    pts = [(t, None if s is None else s - shift) for t, s in pts]
    base = base0 * 2.0 ** (shift / 12.0)
    pts = despike(smooth(pts))
    voiced = [s for _t, s in pts if s is not None]
    if not voiced:
        sys.exit("no voiced frames")

    total = len(data) / rate
    peaks_found = peaks(pts)

    print("%s  %.2f s  %d Hz  base %.1f Hz  range %+.1f..%+.1f st"
          % (args.wav.split("\\")[-1], total, rate, base,
             min(voiced), max(voiced)))
    if args.words:
        print("  rate  %.0f wpm over %d words" % (args.words / (total / 60.0),
                                                  args.words))
    print("  voiced %.0f%% of the file, %d accents"
          % (100.0 * len(voiced) / max(1, len(pts)), len(peaks_found)))
    if peaks_found:
        print("  accents:", " ".join("%.2fs %+.1f" % p for p in peaks_found))
    if args.accents:
        for t, s in peaks_found:
            shape = accent_shape(pts, t, s)
            print("    %5.0f ms  %+4.1f st  rise %4.0f  fall %4.0f  width %4.0f"
                  % (1000 * t, s, shape["rise_ms"], shape["fall_ms"],
                     shape["width_ms"]))
    if args.flat:
        near = sum(1 for s in voiced if abs(s) <= 0.5)
        print("  flat: %.0f%% of voiced frames within half a semitone of the "
              "median" % (100.0 * near / max(1, len(voiced))))

    # declination: a straight line through the contour, semitones per second
    seq = np.array([(t, s) for t, s in pts if s is not None])
    if len(seq) > 10:
        slope, off = np.polyfit(seq[:, 0], seq[:, 1], 1)
        print("  declination %+.2f st/s (from %+.1f to %+.1f predicted)"
              % (slope, off, off + slope * total))

    # the landing: the last voiced value and the deepest point near the end
    tail = [(t, s) for t, s in pts if s is not None and t > total - 0.35]
    if tail:
        deepest = min(s for _t, s in tail)
        print("  last voiced %+.1f st, deepest in the last 350 ms %+.1f st"
              % (tail[-1][1], deepest))

    if args.plot:
        print(plot(pts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
