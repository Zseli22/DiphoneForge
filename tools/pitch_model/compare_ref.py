#!/usr/bin/env python3
"""Reference against model, sentence by sentence.

For each sentence it measures the recording and the model's contour the same
way, and prints the few numbers that decide whether the shape is right: the
range it uses, how many accents, how wide an accent is, and where it lands.

    python tools/pitch_model/compare_ref.py --ref output/pitch_model/IBM_TTS \
        --rate 250
"""

import argparse
import math
import os
import subprocess
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import analyse_ref as ar          # noqa: E402
import pitch_model as pm          # noqa: E402

TEXTS = [
    ("A1", "The cat sat on the mat.", "none"),
    ("A2", "Extraordinary administrators reorganised everything.", "none"),
    ("A3", "We need bread, milk, and eggs.", "none"),
    ("A4", "Are you coming to the meeting this afternoon?", "yesno"),
    ("A5", "Where did you put the keys?", "wh"),
    ("A6", "I think it was him.", "none"),
    ("A7", "Seventeen blackbirds landed on the roof.", "none"),
    ("A8", "Now.", "none"),
    ("A9", "When the program finally opened, the window was empty and "
           "nothing responded.", "none"),
    ("A10", "This is a test of the emergency broadcast system.", "none"),
]

PHO_DUMP = os.path.join(HERE, "pho_dump.exe")
DEFAULT_ENGINE = os.path.join(
    os.environ.get("APPDATA", ""), "nvda", "addons", "espeakWitMbrola",
    "synthDrivers")


def ref_stats(path):
    data, rate = ar.read_wav(path)
    raw = ar.track(data, rate)
    b0 = ar.median_of(raw)
    pts = ar.reject_outliers(ar.to_semitones(raw, b0))
    vals = [s for _t, s in pts if s is not None]
    shift = float(np.median(vals))
    pts = [(t, None if s is None else s - shift) for t, s in pts]
    pts = ar.despike(ar.smooth(pts))
    out = ar.peaks(pts)
    shapes = [ar.accent_shape(pts, t, s) for t, s in out]
    voiced = [s for _t, s in pts if s is not None]
    return {
        "lo": min(voiced), "hi": max(voiced), "accents": len(out),
        "rise": median([s["rise_ms"] for s in shapes]),
        "width": median([s["width_ms"] for s in shapes]),
        "land": voiced[-1],
        "flat": 100.0 * sum(1 for s in voiced if abs(s) <= 0.5) / len(voiced),
    }


def model_stats(text, engine, rate, out_dir, question="none"):
    prefix = os.path.join(out_dir, "_cmp")
    env = dict(os.environ)
    env["PATH"] = engine + os.pathsep + env.get("PATH", "")
    subprocess.run([PHO_DUMP, engine, "mb-usd16", str(rate), prefix, text],
                   env=env, check=True, capture_output=True)
    pho = pm.parse_pho(prefix + ".pho")
    with open(prefix + ".mn", encoding="utf-8", errors="replace") as f:
        mnem = f.read().strip()
    info, _cov = pm.align(pm.split_mnemonics(mnem), pho)
    events, total = pm.find_events(pho, info)
    original, _t = pm.pho_contour(pho)
    suppress = question != "none"
    end_style = "question" if question == "yesno" else "full"
    base = pm.calibrate(events, end_style, pm.median_hz(original, total),
                        total, 1.0, suppress)
    points = pm.contour(events, end_style, base, total_ms=total,
                        suppress_pre_nuclear=suppress)
    # Sample the polyline the same way the recording is sampled, so the
    # accent measurement below is like for like.
    step = 5.0
    pts = []
    k = 0
    while k * step <= total:
        pts.append((k * step / 1000.0, pm.value_at(points, k * step)))
        k += 1
    levels = [v for _t, v in pts]
    centre = median(levels)
    pts = [(t, 12.0 * math.log2(v / centre)) for t, v in pts]
    found = ar.peaks(pts)
    shapes = [ar.accent_shape(pts, t, s) for t, s in found]
    voiced = [s for _t, s in pts]
    return {
        "lo": min(voiced), "hi": max(voiced), "accents": len(found),
        "rise": median([s["rise_ms"] for s in shapes]),
        "width": median([s["width_ms"] for s in shapes]),
        "land": voiced[-1],
        "flat": 100.0 * sum(1 for s in voiced if abs(s) <= 0.5)
                / max(1, len(voiced)),
    }


def median(vals):
    return float(np.median(vals)) if vals else 0.0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--ref", default=os.path.join(
        "output", "pitch_model", "IBM_TTS"))
    ap.add_argument("--rate", type=int, default=250)
    ap.add_argument("--engine", default=DEFAULT_ENGINE)
    args = ap.parse_args(argv)

    print("%-4s %-30s %14s %14s %6s" %
          ("", "sentence", "reference", "model", "acc"))
    print("%-4s %-30s %6s %6s %6s %6s %6s %6s" %
          ("", "", "lo", "hi", "lo", "hi", "ref", "mod"))
    rows = []
    for name, text, question in TEXTS:
        path = os.path.join(args.ref, name + ".wav")
        if not os.path.exists(path):
            continue
        r = ref_stats(path)
        m = model_stats(text, args.engine, args.rate, args.ref, question)
        rows.append((name, r, m))
        print("%-4s %-30.30s %+6.1f %+6.1f %+6.1f %+6.1f %6d %6d" %
              (name, text, r["lo"], r["hi"], m["lo"], m["hi"],
               r["accents"], m["accents"]))

    if rows:
        print()
        print("reference: accents rise %.0f ms, %.0f ms wide, land %+.1f st, "
              "%.0f%% flat"
              % (median([r["rise"] for _n, r, _m in rows]),
                 median([r["width"] for _n, r, _m in rows]),
                 median([r["land"] for _n, r, _m in rows]),
                 median([r["flat"] for _n, r, _m in rows])))
        print("model:     accents rise %.0f ms, %.0f ms wide, land %+.1f st, "
              "%.0f%% flat"
              % (median([m["rise"] for _n, _r, m in rows]),
                 median([m["width"] for _n, _r, m in rows]),
                 median([m["land"] for _n, _r, m in rows]),
                 median([m["flat"] for _n, _r, m in rows])))
        print("reference: range %+.1f st on average"
              % median([r["hi"] - r["lo"] for _n, r, _m in rows]))
        print("model:     range %+.1f st on average"
              % median([m["hi"] - m["lo"] for _n, _r, m in rows]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
