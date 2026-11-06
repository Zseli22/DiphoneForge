#!/usr/bin/env python3
"""
Find vowels that a language's eSpeak voice renders identically.

A recording script built from a symbol inventory often asks for the same sound
twice. eSpeak's General American voice is a good example: ``A:`` and ``0`` are
the same phoneme (PALM and LOT merged), and ``3`` and ``3:`` differ only in
spectral tilt. Recording both halves costs a session and changes nothing the
synthesiser can use, because the frontend cannot ask for a distinction the voice
does not make.

This tool renders every vowel in a fixed consonant frame, compares the renders
with dynamic time warping over MFCC frames, and reports pairs that are the same
sound. With --write it produces a merge map that the prompt generator and the
eSpeak table writer both understand.

It can only find distinctions the synthesiser does not make. A speaker whose
dialect merges two sounds that eSpeak keeps apart, such as cot and caught, has to
decide that by ear; the report shows those near-misses so the decision is an
informed one.

Usage:
    python tools/find_duplicate_vowels.py
    python tools/find_duplicate_vowels.py --threshold 1.0 --write output/vowel_merges.json
    python tools/find_duplicate_vowels.py --voice hr --phonemes languages/hr/phonemes.json
"""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import wave
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
ESPEAK_BIN = SCRIPT_DIR / "espeak-ng" / "espeak-ng.exe"
ESPEAK_DATA = SCRIPT_DIR / "espeak-ng"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Report vowels that the eSpeak voice cannot tell apart."
    )
    parser.add_argument(
        "--phonemes",
        type=Path,
        default=PROJECT_ROOT / "data" / "phonemes.json",
        help="Inventory file with a vowels list.",
    )
    parser.add_argument(
        "--voice",
        default="en-us",
        help="eSpeak voice to probe (default: en-us).",
    )
    parser.add_argument(
        "--onset", default="p", help="Consonant before the vowel (default: p)."
    )
    parser.add_argument(
        "--coda", default="t", help="Consonant after the vowel (default: t)."
    )
    parser.add_argument(
        "--speed", type=int, default=150, help="Speaking rate in wpm (default: 150)."
    )
    parser.add_argument(
        "--pitch", type=int, default=70, help="Base pitch 0-99 (default: 70)."
    )
    parser.add_argument(
        "--threshold", type=float, default=1.2,
        help="DTW distance at or below which two vowels are treated as the same "
             "sound. 0 only merges byte-identical renders (default: 1.2).",
    )
    parser.add_argument(
        "--duration-tolerance", type=float, default=1.15,
        help="Longest duration ratio still treated as the same sound. A short "
             "and a long vowel of the same quality are distinguishable "
             "(default: 1.15).",
    )
    parser.add_argument(
        "--close-count", type=int, default=12,
        help="How many of the closest pairs to print (default: 12).",
    )
    parser.add_argument(
        "--write",
        type=Path,
        default=PROJECT_ROOT / "output" / "vowel_merges.json",
        help="Where to write the merge map. Use an empty string to skip.",
    )
    return parser.parse_args()


def require_numpy():
    try:
        import numpy  # noqa: F401
        import scipy  # noqa: F401
    except ImportError:
        raise SystemExit(
            "this tool needs numpy and scipy; run it with a Python that has them, "
            "for example tools\\mfa-env\\python.exe"
        )


def espeak_env() -> dict:
    env = os.environ.copy()
    env["ESPEAK_DATA_PATH"] = str(ESPEAK_DATA)
    return env


def render(phones: list[str], args: argparse.Namespace):
    import numpy as np

    target = "[[%s]]" % " ".join(phones)
    data = subprocess.run(
        [
            str(ESPEAK_BIN),
            "-v", args.voice,
            "-s", str(args.speed),
            "-p", str(args.pitch),
            "-P", "0",
            "-z",
            "--stdout",
            target,
        ],
        env=espeak_env(),
        check=True,
        capture_output=True,
    ).stdout
    with wave.open(io.BytesIO(data), "rb") as handle:
        rate = handle.getframerate()
        samples = np.frombuffer(
            handle.readframes(handle.getnframes()), dtype=np.int16
        ).astype(np.float64)
    return samples, rate


def mel_filterbank(rate: int, bands: int = 26, nfft: int = 512,
                   lo: float = 80.0, hi: float = 5000.0):
    import numpy as np

    def hz_to_mel(f):
        return 2595.0 * np.log10(1.0 + f / 700.0)

    def mel_to_hz(m):
        return 700.0 * (10.0 ** (m / 2595.0) - 1.0)

    edges = np.linspace(hz_to_mel(lo), hz_to_mel(hi), bands + 2)
    freqs = np.fft.rfftfreq(nfft, 1.0 / rate)
    bank = np.zeros((bands, len(freqs)))
    for index in range(bands):
        left, centre, right = mel_to_hz(edges[index]), mel_to_hz(edges[index + 1]), mel_to_hz(edges[index + 2])
        bank[index] = np.clip(
            np.minimum((freqs - left) / (centre - left), (right - freqs) / (right - centre)),
            0.0, None,
        )
    return bank


def mfcc_frames(samples, rate: int):
    import numpy as np
    from scipy.fftpack import dct

    nfft = 512
    win = int(0.025 * rate)
    hop = int(0.005 * rate)
    bank = mel_filterbank(rate)
    frames = []
    for start in range(0, len(samples) - win, hop):
        frame = samples[start:start + win] * np.hamming(win)
        if np.sqrt(np.mean(frame ** 2)) < 30.0:
            continue
        power = np.abs(np.fft.rfft(frame, nfft)) ** 2
        frames.append(dct(np.log(bank @ power + 1e-9), type=2, norm="ortho")[1:13])
    return np.array(frames)


def dtw(a, b) -> float:
    import numpy as np

    n, m = len(a), len(b)
    cost = np.full((n + 1, m + 1), np.inf)
    cost[0, 0] = 0.0
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            cost[i, j] = np.linalg.norm(a[i - 1] - b[j - 1]) + min(
                cost[i - 1, j], cost[i, j - 1], cost[i - 1, j - 1]
            )
    return float(cost[n, m] / (n + m))


def main() -> None:
    args = parse_args()
    require_numpy()

    if not ESPEAK_BIN.exists():
        raise SystemExit(f"eSpeak NG not found: {ESPEAK_BIN}")
    if not args.phonemes.exists():
        raise SystemExit(f"inventory not found: {args.phonemes}")

    spec = json.loads(args.phonemes.read_text(encoding="utf-8"))
    inventory = spec["vowels"]

    features: dict[str, object] = {}
    durations: dict[str, float] = {}
    for vowel in inventory:
        samples, rate = render([args.onset, "'" + vowel, args.coda], args)
        features[vowel] = mfcc_frames(samples, rate)
        durations[vowel] = len(samples) / rate

    pairs = []
    for index, first in enumerate(inventory):
        for second in inventory[index + 1:]:
            distance = dtw(features[first], features[second])
            ratio = max(durations[first], durations[second]) / min(
                durations[first], durations[second]
            )
            pairs.append((distance, ratio, first, second))
    pairs.sort()

    identical = [
        (distance, ratio, a, b)
        for distance, ratio, a, b in pairs
        if distance < 0.05
    ]
    merged = [
        (distance, ratio, a, b)
        for distance, ratio, a, b in pairs
        if distance <= args.threshold and ratio <= args.duration_tolerance
    ]

    print(f"{args.voice}: {len(inventory)} vowels, probed in "
          f"{args.onset!r} _ {args.coda!r} at {args.speed} wpm")
    print("\nclosest pairs (DTW per frame; 0 is the same render, lower is closer)")
    for distance, ratio, first, second in pairs[:args.close_count]:
        if distance < 0.05:
            verdict = "identical"
        elif any((distance, ratio, first, second) == entry for entry in merged):
            verdict = "same sound"
        elif distance <= args.threshold:
            verdict = "near, but different length"
        else:
            verdict = "distinct"
        print(f"  {distance:6.2f}  {first:>3} vs {second:<3}  "
              f"length x{ratio:4.2f}  {verdict}")

    # Keep the earlier symbol in the inventory as the representative, so the
    # choice is stable and usually the simpler one.
    representative: dict[str, str] = {}
    for _, _, first, second in identical:
        rep = representative.get(first, first)
        representative[second] = rep
        representative[first] = rep
    for _, _, first, second in merged:
        rep_a = representative.get(first, first)
        rep_b = representative.get(second, second)
        if rep_a == rep_b:
            continue
        keep, drop = sorted((rep_a, rep_b), key=inventory.index)
        representative[drop] = keep

    merges = {source: target for source, target in representative.items()
              if source != target}

    if merges:
        print("\nmerged (the recording script can skip the key; the frontend "
              "maps it to the value):")
        for source, target in sorted(merges.items(), key=lambda kv: inventory.index(kv[0])):
            print(f"  {source:>3} -> {target}")
    else:
        print("\nno vowels are the same sound; nothing to merge")

    close = [
        (distance, ratio, a, b)
        for distance, ratio, a, b in pairs
        if args.threshold < distance <= args.threshold * 2
    ]
    if close:
        print("\nclose but kept separate; listen before deciding:")
        for distance, ratio, first, second in close[:args.close_count]:
            print(f"  {distance:6.2f}  {first:>3} vs {second:<3}  "
                  f"length x{ratio:4.2f}")

    if args.write and str(args.write):
        args.write.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "voice": args.voice,
            "inventory": str(args.phonemes),
            "probe": {"onset": args.onset, "coda": args.coda,
                      "speed": args.speed, "pitch": args.pitch, "range": 0},
            "threshold": args.threshold,
            "merges": merges,
            "closest": [
                {"a": a, "b": b, "distance": round(d, 3), "length_ratio": round(r, 3)}
                for d, r, a, b in pairs[:args.close_count]
            ],
        }
        args.write.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"\nwrote {args.write}")


if __name__ == "__main__":
    main()
