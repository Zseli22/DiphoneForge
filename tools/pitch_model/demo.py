#!/usr/bin/env python3
"""Make an A/B pair: eSpeak's own intonation against the pitch model.

For one piece of text it writes three files under the output folder:

    <name>.pho         what eSpeak would hand MBROLA, untouched
    <name>.model.pho   the same phones and durations, model contour on top
    <name>.base.wav    both rendered, same durations, different melody
    <name>.model.wav

Nothing here is specific to MBROLA except the last step: the model itself
answers a contour, and rendering it into "position, hertz" pairs is one
function in pitch_model.py.

    python tools/pitch_model/demo.py --name fox "The quick brown fox jumps."

The eSpeak and MBROLA it drives are the ones the add-on already has, so the
voice, the durations and the database are exactly what NVDA would use.
"""

import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
DEFAULT_ENGINE = os.path.join(
    os.environ.get("APPDATA", ""), "nvda", "addons", "espeakWitMbrola",
    "synthDrivers")
DEFAULT_OUT = os.path.join(ROOT, "output", "pitch_model")

PHO_DUMP = os.path.join(HERE, "pho_dump.exe")


def run(cmd, env=None):
    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if r.returncode != 0:
        sys.exit("failed: %s\n%s%s" % (" ".join(cmd), r.stdout, r.stderr))
    return r.stdout


WH_WORDS = {"what", "where", "when", "why", "how", "who", "whom", "whose",
            "which"}


def question_from_text(text):
    """A question carries one accent, and only a yes/no question rises.

    This is the one piece of the text analysis the model needs: which of the
    two shapes a '?' asked for.
    """
    stripped = text.strip()
    if not stripped.endswith("?"):
        return "none"
    first = stripped.split()[0].lower().strip("\"'(") if stripped else ""
    return "wh" if first in WH_WORDS else "yesno"


def pho_stats(path):
    """What a .pho says the pitch does, exactly: min, median, max, and the
    last value, which is the one that decides how the sentence sounds."""
    vals = []
    last = None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            for tail in parts[2:]:
                nums = [float(x) for x in tail.replace(",", " ").split()]
                for i in range(0, len(nums) - 1, 2):
                    vals.append(nums[i + 1])
                    last = nums[i + 1]
    if not vals:
        return None
    vals.sort()
    return vals[0], vals[len(vals) // 2], vals[-1], last


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("text", help="the sentence to speak")
    ap.add_argument("--name", default="case", help="the file stem to write")
    ap.add_argument("--rate", type=int, default=175,
                    help="eSpeak words a minute (default 175, its own base)")
    ap.add_argument("--voice", default="mb-usd16", help="eSpeak voice")
    ap.add_argument("--db", default="usd16", help="MBROLA database name")
    ap.add_argument("--end", default="full",
                    choices=["full", "continuation", "question"],
                    help="how the last phrase lands (inferred for a "
                         "question when this is left alone)")
    ap.add_argument("--question", default=None,
                    choices=["none", "yesno", "wh"],
                    help="override what the text says about the question")
    ap.add_argument("--base", type=float, default=None,
                    help="base pitch in Hz (default: the voice's own level)")
    ap.add_argument("--range", type=float, default=1.0,
                    help="scale the melody: 1.0 measured, 0.7 flatter")
    ap.add_argument("--engine", default=DEFAULT_ENGINE)
    ap.add_argument("--out", default=DEFAULT_OUT)
    args = ap.parse_args(argv)

    engine = args.engine
    mbrola = os.path.join(engine, "mbrola.exe")
    database = os.path.join(engine, "espeak-ng-data", "mbrola", args.db)
    for needed in (PHO_DUMP, mbrola, database):
        if not os.path.exists(needed):
            sys.exit("missing: %s" % needed)
    os.makedirs(args.out, exist_ok=True)

    # The DLL wants mbrola.dll beside itself on the search path.
    env = dict(os.environ)
    env["PATH"] = engine + os.pathsep + env.get("PATH", "")

    prefix = os.path.join(args.out, args.name)
    pho = prefix + ".pho"
    mn = prefix + ".mn"
    model_pho = prefix + ".model.pho"

    print("text:", args.text)
    run([PHO_DUMP, engine, args.voice, str(args.rate), prefix, args.text],
        env=env)
    with open(mn, encoding="utf-8", errors="replace") as f:
        mnem = f.read().strip()
    print("phonemes:", mnem)

    cmd = [sys.executable, os.path.join(HERE, "pitch_model.py"), pho,
           "--mnemonics", mnem, "--out", model_pho, "--end", args.end,
           "--range", str(args.range)]
    question = args.question if args.question is not None else \
        question_from_text(args.text)
    if question != "none":
        cmd += ["--question", question]
        if question == "yesno" and args.end == "full":
            cmd[cmd.index("--end") + 1] = "question"
    if args.base:
        cmd += ["--base", str(args.base)]
    run(cmd)

    for src, dst in ((pho, prefix + ".base.wav"),
                     (model_pho, prefix + ".model.wav")):
        run([mbrola, database, src, dst])

    print("wrote:")
    for suffix in (".pho", ".model.pho", ".base.wav", ".model.wav"):
        path = prefix + suffix
        if os.path.exists(path):
            print("  %s  (%d bytes)" % (path, os.path.getsize(path)))

    print("pitch, as the .pho says it:")
    for label, path in (("eSpeak", pho), ("model ", model_pho)):
        s = pho_stats(path)
        if s:
            print("  %s  min %5.0f  median %5.0f  max %5.0f  last %5.0f Hz"
                  % (label, s[0], s[1], s[2], s[3]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
