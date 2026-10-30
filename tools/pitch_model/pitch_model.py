#!/usr/bin/env python3
"""A portable intonation model, and a writer that puts it into a .pho.

The point of this file is that nothing in it is specific to any synthesis
engine.  It takes a list of phones with durations, a set of linguistic
events found among them (stressed syllables, word edges, phrase edges) and
one number for the speaking voice, and it answers a piecewise-linear F0
contour in hertz.  Rendering that contour into MBROLA's "position, hertz"
pairs is a separate, small step at the bottom; an engine that wants frames,
a tune table or a breakpoint list instead only needs its own writer.

The shape is measured, not invented.  The numbers come from the contour the
IBM engine draws in Eloquence, which is a straight-line breakpoint list and
not a filtered curve.  Measured on that engine's own output:

    the peak sits 0.47 through the stressed vowel   (quartiles 0.41, 0.63)
    the rise lasts 124 ms                           (a median over 1200 words)
    the final fall lasts 58 ms and lands 3 ms before the end
    the first fall ends where the last vowel ends, or 58 ms before the
      landing, whichever comes first
    the start is higher when the first syllable is accented, lower when not

Everything is expressed in semitones about the voice's base pitch, so the
same model works on a 70 Hz voice and a 220 Hz one.  The absolute levels are
the measured ones divided by that engine's 110 Hz default voice.

This is an approximation by intent.  It reproduces the *shape* of that
intonation, not the text analysis that decides which word to accent, which
is where most of that engine's identity actually lives.
"""

import argparse
import math
import re
import sys

# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------

# All levels are semitones above the voice's base pitch.  A2C is "accent to
# contour": the level the contour sits at before the rise, the peak it rises
# to, and the level it falls to afterwards.
START_ACCENTED = 1.7      # first syllable of the phrase carries the accent
START_PLAIN = -0.9        # it does not
PEAK_FIRST = 3.0          # first accent's peak
PEAK_STEP = -0.35         # each later accent peaks a little lower
FLOOR_FIRST = -0.9        # the level the first accent falls back to
FLOOR_STEP = -0.45        # and each later one falls a little lower
FALL_LEVEL = -5.7         # where the first of the two final falls lands
LAND = -7.5               # and where the word finally lands
QUESTION_END = 4.0        # a yes/no question rises to here instead

RISE_MS = 100             # length of an accent's rise, measured
FALL_MS = 140             # and of an accent's fall, measured
PEAK_AT = 0.47            # how far through the stressed vowel the peak sits
SECOND_FALL_MS = 58       # length of the reserved landing
LAND_BEFORE_END_MS = 3    # how far before the phrase end it lands

# A pre-nuclear accent in a yes/no question is barely there: measured on
# eight questions, the whole clause carried one accent and the rest of the
# contour moved by about a semitone.  Rather than drop the events, which
# would leave a flat line, they are scaled down.
QUESTION_PRE_NUCLEAR = 0.15

END_STYLES = {
    "full": LAND,
    "continuation": -3.4,
    "question": QUESTION_END,
}


def hz(base, semitones):
    return base * (2.0 ** (semitones / 12.0))


class Event:
    """One accent: a stressed vowel, the word it sits in, its phrase."""

    __slots__ = ("vowel_start", "vowel_end", "word_start", "word_end",
                 "phrase_end", "phrase_vowel_end", "phrase_last", "final",
                 "first_in_word", "first_in_phrase", "index", "primary")

    def __init__(self, **kw):
        for k, v in kw.items():
            setattr(self, k, v)


def contour(events, end_style, base, total_ms=None, range_scale=1.0,
            suppress_pre_nuclear=False):
    """The whole contour as a list of (millisecond, hertz) breakpoints.

    Every accent contributes a hold, a rise and a fall, and the last one in
    each phrase contributes a reserved landing instead of an ordinary fall.
    Points are sorted by time; where two land on the same millisecond the
    later one wins, which is what makes a rise start inside the consonant in
    front of its vowel rather than fighting the word edge.

    `range_scale` multiplies every level, which is the one knob a listener
    reaches for: 1.0 is the measured amount, 0.7 flattens it, 1.3 exaggerates
    it.
    """
    if not events:
        return [(0, hz(base, START_PLAIN * range_scale))]

    def level(x):
        return hz(base, x * range_scale)

    points = []

    for n, ev in enumerate(events):
        peak = PEAK_FIRST + PEAK_STEP * n
        floor = START_ACCENTED if (n == 0 and ev.first_in_phrase
                                   and ev.first_in_word) else \
            FLOOR_FIRST + FLOOR_STEP * n

        # In a question only the nuclear accent is a real accent: eight
        # questions measured carried one accent each and moved about a
        # semitone elsewhere.
        scale = 1.0
        if suppress_pre_nuclear and not ev.final:
            scale = QUESTION_PRE_NUCLEAR
        peak = floor + (peak - floor) * scale

        peak_t = ev.vowel_start + PEAK_AT * (ev.vowel_end - ev.vowel_start)
        # The rise is a fixed length and is allowed to begin inside the
        # consonant in front of the vowel, which is where the source puts it.
        # Clamping it to the vowel turned every short syllable into a chirp.
        rise_t = max(0.0, peak_t - RISE_MS)

        # A yes/no question does not fall at all on its last word: the whole
        # word is the rise, which is why it sounds like a question rather
        # than like a statement with a twitch on the end.
        if ev.final and end_style == "question":
            points.append((ev.word_start, level(floor)))
            points.append((ev.phrase_end, level(QUESTION_END)))
            continue

        # The level drifts down to the floor rather than holding on it: a
        # contour that sits still between accents is the single most
        # mechanical thing this model did.
        points.append((rise_t, level(floor)))
        points.append((peak_t, level(peak)))

        if not ev.phrase_last:
            points.append((min(ev.word_end, peak_t + FALL_MS),
                           level(floor + FLOOR_STEP)))
        elif not ev.final:
            # A phrase that is going to be followed by more ends high: the
            # fall is the whole gesture, with no reserved landing.
            points.append((ev.phrase_end,
                           level(END_STYLES["continuation"])))
        else:
            # The reserved landing.  The first fall ends with the last vowel,
            # but never so late that the second fall has no room.
            land_t = max(ev.vowel_end, ev.phrase_end - LAND_BEFORE_END_MS)
            fall2_start = max(ev.phrase_vowel_end, land_t - SECOND_FALL_MS)
            if fall2_start > ev.phrase_vowel_end:
                points.append((ev.phrase_vowel_end, level(FALL_LEVEL)))
            points.append((fall2_start, level(FALL_LEVEL)))

            if end_style == "question":
                points.append((land_t, level(QUESTION_END)))
            else:
                end_level = END_STYLES.get(end_style, LAND)
                points.append((land_t, level(end_level)))

    points.sort()
    # A later point at the same time replaces an earlier one.
    out = []
    for t, f in points:
        if out and abs(out[-1][0] - t) < 1e-6:
            out[-1] = (t, f)
        else:
            out.append((t, f))
    # Nothing may run past the utterance.
    if total_ms is not None:
        out = [(min(t, total_ms), f) for t, f in out]
        if out[-1][0] < total_ms:
            out.append((total_ms, out[-1][1]))
    return out


def value_at(points, t):
    """Linear interpolation, which is what the contour is made of."""
    if not points:
        return 0.0
    if t <= points[0][0]:
        return points[0][1]
    for (t0, f0), (t1, f1) in zip(points, points[1:]):
        if t0 <= t <= t1:
            if t1 == t0:
                return f1
            return f0 + (f1 - f0) * (t - t0) / (t1 - t0)
    return points[-1][1]


def mean_hz(points, total):
    """The average the contour actually spends, weighted by time."""
    if total <= 0 or not points:
        return 0.0
    steps = max(1, int(total / 5))
    acc = 0.0
    for k in range(steps):
        acc += value_at(points, (k + 0.5) * total / steps)
    return acc / steps


def median_hz(points, total):
    """The level the contour spends half its time above."""
    if total <= 0 or not points:
        return 0.0
    steps = max(1, int(total / 5))
    vals = sorted(value_at(points, (k + 0.5) * total / steps)
                  for k in range(steps))
    return vals[len(vals) // 2]


def calibrate(events, end_style, target_median, total, range_scale=1.0,
              suppress_pre_nuclear=False):
    """The base pitch that puts the model's median where the original's was.

    The contour is linear in the base, so this is a division rather than a
    search.  Calibrating on the median rather than the mean matters: a
    contour that holds still near its floor spends more time low than high,
    and matching the mean then lifts the whole thing and shallows the
    landing.
    """
    probe = contour(events, end_style, 1.0, total, range_scale,
                    suppress_pre_nuclear)
    got = median_hz(probe, total)
    if got <= 0:
        return 100.0
    return target_median / got


# ---------------------------------------------------------------------------
# The .pho side: reading phones, writing pitch pairs
# ---------------------------------------------------------------------------

# Every name the US English voice can put in a .pho.  Longest first, because
# eSpeak writes mnemonics as one run of characters with no separators.
PHONES = [
    "i:", "u:", "0:", "3:", "@:", "A:", "O:", "E:", "aI", "aU", "OI",
    "eI", "oU", "i@", "e@", "U@", "@L", "@2", "dZ", "tS", "l/", "n/",
    "r-", "z/", "s/", "t/", "d/", "N", "T", "D", "S", "Z", "R", "L",
    "J", "W", "C", "X", "G", "H", "P", "B", "F", "V", "M",
    "i", "I", "E", "{", "a", "0", "U", "u", "V", "3", "@", "O", "A",
    "p", "b", "t", "d", "k", "g", "f", "v", "s", "z", "S", "Z", "h",
    "m", "n", "N", "l", "r", "w", "j", "_",
]

VOWELS = {
    "i:", "u:", "0:", "3:", "@:", "A:", "O:", "E:", "aI", "aU", "OI",
    "eI", "oU", "i@", "e@", "U@", "i", "I", "E", "{", "a", "0", "U", "u",
    "V", "3", "@", "O", "A", "@L", "@2",
}

_PHONE_SORTED = sorted(set(PHONES), key=len, reverse=True)


def parse_pho(path):
    """[(phone, ms, [(pos, hz), ...]), ...], in order."""
    lines = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            raw = raw.rstrip("\r\n")
            if not raw.strip():
                continue
            parts = raw.split("\t")
            name = parts[0].strip()
            try:
                ms = float(parts[1])
            except (IndexError, ValueError):
                continue
            pairs = []
            for tail in parts[2:]:
                nums = re.findall(r"-?\d+(?:\.\d+)?", tail)
                for i in range(0, len(nums) - 1, 2):
                    pairs.append((float(nums[i]), float(nums[i + 1])))
            lines.append([name, ms, pairs])
    return lines


def write_pho(path, lines, points):
    """Write the phones back with the contour sampled into each one."""
    t = 0.0
    out = []
    for name, ms, _old in lines:
        t0, t1 = t, t + ms
        marks = [(t0, 0.0), (t1, 100.0)]
        for pt, _f in points:
            if t0 < pt < t1:
                marks.append((pt, 100.0 * (pt - t0) / ms if ms else 0.0))
        marks.sort(key=lambda m: (m[0], m[1]))
        pairs = []
        for when, pct in marks:
            pct = min(100.0, max(0.0, pct))
            pairs.append((pct, value_at(points, when)))
        line = "%s\t%d" % (name, int(round(ms)))
        for pct, f in pairs:
            line += "\t%d %d" % (int(round(pct)), int(round(f)))
        out.append(line)
        t = t1
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write("\n".join(out) + "\n")
    return t


# ---------------------------------------------------------------------------
# Finding the events
# ---------------------------------------------------------------------------

def split_mnemonics(text):
    """Turn '-x' output into words of (phone, stress) pairs.

    eSpeak writes one run of characters per word, with ' before a primary
    stressed vowel and , before a secondary one.  Spaces separate words.
    """
    words = []
    for word in text.split():
        toks = []
        i, stress = 0, 0
        while i < len(word):
            ch = word[i]
            if ch in "'\",":
                stress = 1 if ch == "'" else 2
                i += 1
                continue
            for cand in _PHONE_SORTED:
                if word.startswith(cand, i):
                    toks.append([cand, stress])
                    stress = 0
                    i += len(cand)
                    break
            else:
                i += 1
        if toks:
            words.append(toks)
    return words


def _norm(name):
    """A phone name with the things two tables spell differently removed.

    The .pho carries the MBROLA table's names and the mnemonics carry
    eSpeak's; they differ in suffixes (`i`/`i:`), in a dropped stress
    variant (`@2` for `@`) and in the coda marks (`l/`, `r-`).  None of that
    changes which phone it is, so alignment ignores it.
    """
    name = name.rstrip(":").rstrip("/").rstrip("-")
    if name == "@2":
        name = "@"
    return name


def align(mn_words, pho):
    """Map mnemonic tokens onto .pho lines.

    Both are the same utterance in the same order, so this is a sequence
    match on names rather than a phonetic comparison.  In practice the only
    mismatches are names the MBROLA table rewrites, which are dropped.
    """
    import difflib

    flat = []
    for w, toks in enumerate(mn_words):
        for n, (name, stress) in enumerate(toks):
            flat.append((name, stress, w, n == 0))

    mnames = [_norm(t[0]) for t in flat]
    pnames = [_norm(line[0]) for line in pho]

    info = [None] * len(pho)
    matched = 0
    sm = difflib.SequenceMatcher(a=mnames, b=pnames, autojunk=False)
    for a, b, size in sm.get_matching_blocks():
        for k in range(size):
            name, stress, w, first = flat[a + k]
            info[b + k] = {"stress": stress, "word": w, "first": first,
                           "name": name}
            matched += 1
    coverage = matched / max(1, sum(1 for line in pho if line[0] != "_"))
    return info, coverage


def find_events(pho, info):
    """Build the accent events, and the phrase ends, from the alignment."""
    t = 0.0
    starts, ends = [], []
    for name, ms, _pairs in pho:
        starts.append(t)
        ends.append(t + ms)
        t += ms
    total = t

    # Phrases break at a silence long enough to be a pause rather than a gap.
    phrases = []
    start = 0
    for i, (name, ms, _p) in enumerate(pho):
        if name == "_" and ms >= 40.0 and i > start:
            phrases.append((start, i - 1))
            start = i
    if start < len(pho):
        phrases.append((start, len(pho) - 1))

    events = []
    for p_start, p_end in phrases:
        # A phrase ending in a pause lands before the pause, not inside it.
        phrase_end = starts[p_end] + pho[p_end][1]
        if pho[p_end][0] == "_":
            phrase_end = starts[p_end]

        # Group the phones of this phrase into words.
        words = {}
        for i in range(p_start, p_end + 1):
            meta = info[i] if info[i] else None
            if meta is None:
                continue
            words.setdefault(meta["word"], []).append(i)

        n_vowel_in_phrase = 0
        phrase_events = []
        for w, idxs in sorted(words.items()):
            vowels = [(i, info[i]["stress"]) for i in idxs
                      if pho[i][0] in VOWELS]
            if not vowels:
                continue
            stressed = [v for v in vowels if v[1]]
            target = stressed[0] if stressed else vowels[0]
            stress = target[1]
            if not stress:
                n_vowel_in_phrase += 1
                continue
            i = target[0]
            first_in_word = all(
                pho[j][0] not in VOWELS for j in idxs[:idxs.index(i)])
            phrase_events.append(Event(
                vowel_start=starts[i], vowel_end=ends[i],
                word_start=starts[idxs[0]], word_end=ends[idxs[-1]],
                phrase_end=phrase_end, phrase_vowel_end=0.0,
                phrase_last=False, final=False, first_in_word=first_in_word,
                first_in_phrase=(n_vowel_in_phrase == 0),
                index=w, primary=(stress == 1)))
            n_vowel_in_phrase += 1

        if phrase_events:
            phrase_vowel_end = max(ev.vowel_end for ev in phrase_events)
            for ev in phrase_events:
                ev.phrase_vowel_end = phrase_vowel_end
            phrase_events[-1].phrase_last = True
            events.extend(phrase_events)

    if events:
        events[-1].final = True

    return events, total


def base_from_pho(pho, fallback=100.0):
    """The voice's own level, taken from the pitch the file already carries.

    That keeps the model's mean pitch where the caller put it, so an A/B
    against the engine's own contour is a fair comparison and a female voice
    stays female.
    """
    vals = [f for _n, _ms, pairs in pho for _p, f in pairs]
    if not vals:
        return fallback
    vals.sort()
    return vals[len(vals) // 2]


def pho_contour(pho):
    """The pitch the file already carries, as (absolute ms, hertz) points."""
    t = 0.0
    out = []
    for _name, ms, pairs in pho:
        for pct, f in pairs:
            out.append((t + ms * pct / 100.0, f))
        t += ms
    out.sort()
    return out, t


# ---------------------------------------------------------------------------

def plot(original, model, total, width=74, height=15):
    """Two contours on one pair of axes: 'o' is what the file had, '#' the
    model.  Crude, but it shows the shape better than a column of numbers."""
    lo = min([f for _t, f in original + model] or [80])
    hi = max([f for _t, f in original + model] or [140])
    lo = math.floor(lo / 10) * 10
    hi = math.ceil(hi / 10) * 10
    if hi <= lo:
        hi = lo + 10

    def cell(t, f):
        x = int(round((width - 1) * t / total)) if total else 0
        y = int(round((height - 1) * (f - lo) / (hi - lo)))
        return min(width - 1, max(0, x)), min(height - 1, max(0, y))

    grid = [[" "] * width for _ in range(height)]
    for k in range(width * 2):
        t = total * k / (width * 2 - 1)
        x, y = cell(t, value_at(original, t))
        grid[y][x] = "o"
    for k in range(width * 2):
        t = total * k / (width * 2 - 1)
        x, y = cell(t, value_at(model, t))
        grid[y][x] = "#"

    out = []
    for r, row in enumerate(reversed(grid)):
        hz = hi - (hi - lo) * r / (height - 1)
        out.append("%4.0f |%s" % (hz, "".join(row)))
    out.append("     +" + "-" * width)
    out.append("      0" + " " * (width - 12) + "%.1f s" % (total / 1000.0))
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("pho", help="input .pho")
    ap.add_argument("--mnemonics", required=True,
                    help="eSpeak -x output for the same text")
    ap.add_argument("--out", required=True, help="output .pho")
    ap.add_argument("--base", type=float, default=None,
                    help="base pitch in Hz (default: the voice's median)")
    ap.add_argument("--range", type=float, default=1.0,
                    help="scale the melody: 1.0 measured, 0.7 flatter, 1.3 wider")
    ap.add_argument("--no-match", action="store_true",
                    help="do not move the base to hold the mean pitch")
    ap.add_argument("--end", default="full",
                    choices=["full", "continuation", "question"],
                    help="how the last phrase ends")
    ap.add_argument("--question", default="none",
                    choices=["none", "yesno", "wh"],
                    help="a question carries one accent, not four")
    ap.add_argument("--debug", action="store_true",
                    help="print the contour and the events")
    ap.add_argument("--plot", action="store_true",
                    help="draw the original and the model contour")
    args = ap.parse_args(argv)

    pho = parse_pho(args.pho)
    words = split_mnemonics(args.mnemonics)
    info, coverage = align(words, pho)
    if coverage < 0.85:
        sys.exit("only %.0f%% of the phones lined up; check the mnemonics"
                 % (coverage * 100))

    events, total = find_events(pho, info)
    end_style = args.end
    suppress = args.question != "none"
    if args.question == "yesno":
        end_style = "question"

    original, _t = pho_contour(pho)
    target = median_hz(original, total) if original else None

    if args.base:
        base = args.base
    elif target and not args.no_match:
        base = calibrate(events, end_style, target, total, args.range,
                         suppress)
    else:
        base = base_from_pho(pho)

    points = contour(events, end_style, base, total_ms=total,
                     range_scale=args.range,
                     suppress_pre_nuclear=suppress)
    ms = write_pho(args.out, pho, points)

    if args.debug:
        print("base %.1f Hz, %d events, %.0f ms, end=%s"
              % (base, len(events), ms, end_style))
        if target:
            print("mean: original %.1f Hz, model %.1f Hz"
                  % (target, mean_hz(points, total)))
        for ev in events:
            print("  accent at %5.0f-%5.0f ms  word %5.0f-%5.0f  %s"
                  % (ev.vowel_start, ev.vowel_end, ev.word_start,
                     ev.word_end, "primary" if ev.primary else "secondary"))
        print("  contour:", " ".join("%.0f:%.0f" % p for p in points))
    if args.plot:
        print(plot(original, points, total))
    return 0


if __name__ == "__main__":
    sys.exit(main())
