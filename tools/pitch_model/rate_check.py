#!/usr/bin/env python3
"""What the add-on's rate slider actually delivers, as against what it asks for.

The driver turns the slider into a words-a-minute number and the host then
rewrites the .pho durations to hit it, with a floor under each phone so a
vowel cannot vanish.  The floors mean the delivered rate is not the requested
one at the top of the range, and this measures by how much on real text.

    python tools/pitch_model/rate_check.py --rate 70 "some text"
"""

import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PHO_DUMP = os.path.join(HERE, "pho_dump.exe")
DEFAULT_ENGINE = os.path.join(
    os.environ.get("APPDATA", ""), "nvda", "addons", "espeakWitMbrola",
    "synthDrivers")

BASE_RATE = 175
VOWEL_FLOOR = 25
CONSONANT_FLOOR = 15
PAUSE_FLOOR = 8
VOWELS = {"a", "A", "A:", "E", "I", "O", "U", "@", "3", "3:", "i:", "u:",
          "aI", "aU", "eI", "OI", "oU", "0", "0:"}


def slider_to_target(rate):
    """The driver's own mapping, from espeakwithmbrola.py."""
    rate = max(0, min(100, rate))
    if rate <= 40:
        return int(round(80 + rate * (250 - 80) / 40))
    return int(round(250 + (rate - 40) * (610 - 250) / 60))


def read_pho(path):
    out = []
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 2:
                continue
            try:
                out.append((parts[0], int(float(parts[1]))))
            except ValueError:
                continue
    return out


def deliver(pho, target):
    """The host's adjustPho, exactly: scale, round, floor by phone kind."""
    factor = BASE_RATE / max(1, target)
    total = 0
    floored = 0
    for phone, ms in pho:
        want = max(1, int(round(ms * factor)))
        floor = (PAUSE_FLOOR if phone == "_"
                 else VOWEL_FLOOR if phone in VOWELS else CONSONANT_FLOOR)
        got = max(floor, want)
        if got > want:
            floored += 1
        total += got
    return total, floored


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("text")
    ap.add_argument("--rate", type=int, default=70)
    ap.add_argument("--engine", default=DEFAULT_ENGINE)
    args = ap.parse_args(argv)

    words = len(args.text.split())
    prefix = os.path.join(os.environ.get("TEMP", "."), "rate_check")
    env = dict(os.environ)
    env["PATH"] = args.engine + os.pathsep + env.get("PATH", "")
    subprocess.run([PHO_DUMP, args.engine, "mb-usd16", str(BASE_RATE),
                    prefix, args.text], env=env, check=True)
    pho = read_pho(prefix + ".pho")

    target = slider_to_target(args.rate)
    base_ms = sum(ms for _p, ms in pho)
    got_ms, floored = deliver(pho, target)

    for label, ms in (("as eSpeak speaks it (175 wpm)", base_ms),
                      ("as delivered after the rewrite", got_ms)):
        mins = ms / 60000.0
        print("%-34s %6d ms  %6.1f wpm for %d words"
              % (label, ms, words / mins if mins else 0, words))
    print("slider %d asks for %d wpm; factor %.3f; %d of %d phones hit a floor"
          % (args.rate, target, BASE_RATE / target, floored, len(pho)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
