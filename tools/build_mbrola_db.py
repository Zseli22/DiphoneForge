#!/usr/bin/env python3
"""Build the full MBROLA database from the recorded diphone units.

Steps:
  1. Select one take per recorded diphone label.
  2. Derive the word-edge units (_X, X_) for every phone from room tone.
  3. Fill unrecorded phone pairs by splicing two recorded half-diphones.
  4. Resynthesize every unit through the MBROLATOR analysis chain.
  5. Pack the database with database_build and validate it with MBROLA.

MBROLA aborts a whole utterance when a diphone is missing, so the database is
built for the complete phone-pair matrix.  Spliced units are recorded in the
manifest and in fallback_pairs.csv so they can be replaced by real recordings.

The original recordings under Voice_audio/ are read only.
"""

from __future__ import annotations

import argparse
import array
import collections
import csv
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import soundfile as sf

PROJECT_ROOT = Path(__file__).resolve().parents[1]

UNIT_GRID = PROJECT_ROOT / "output" / "unit_grid" / "unit_grid.csv"
SEGMENTS = PROJECT_ROOT / "output" / "segments" / "segments.csv"
UNIT_AUDIT = PROJECT_ROOT / "output" / "unit_quality" / "unit_audit.csv"
VOICE_AUDIO = PROJECT_ROOT / "Voice_audio"

ANAF0 = PROJECT_ROOT / "tools" / "mbrolator-src" / "AnaMBE" / "Obj" / "anaf0.exe"
ANAMBE = PROJECT_ROOT / "tools" / "mbrolator-src" / "AnaMBE" / "Obj" / "anambe.exe"
RESYNTH = PROJECT_ROOT / "tools" / "mbrolator-src" / "Resynthesis" / "resynth.exe"
DATABASE_BUILD = PROJECT_ROOT / "tools" / "mbrolator-src" / "Resynthesis" / "database_build.exe"
MBROLA = PROJECT_ROOT / "tools" / "mbrola-src" / "Bin" / "mbrola.exe"

# The analysis programs are built for 16 kHz.  For a 22.05 kHz database the
# audio is analyzed as if it were 16 kHz (see the MBROLATOR README addendum)
# and only database_build is told the real rate.
F0_PARAMS = """Freq= 16000
FrameLength= 512
FrameShift= 80
"""
SYN_PARAMS = """Freq=16000
LastPhaseReset=10
VUVLimTra=0.55
VUVLimVoiced=1.2
ElimTrans=5e8
ElimVoiced=5e8
"""
DATABASE_VERSION = "2.069"
DATABASE_CODING = 1
# MBROLA's overlap and transition smoothing can exceed the unit peak by a
# couple of dB; 0.8 keeps the output just below full scale without clipping.
DATABASE_VOLUME = 0.8
RESYNTH_GAIN = 3.5
MAX_PEAK_FRACTION = 0.92

PAD = 0.12              # analysis context beyond a unit span
SILENCE_HALF = 0.10     # silence half of a word-edge unit
TONE_LENGTH = 0.24      # room tone used when a phone cannot sit at a word edge
XFADE = 0.006           # splice crossfade


@dataclass(frozen=True)
class RateConfig:
    rate: int
    mbe_params: str
    period: int


RATE_CONFIGS: Dict[int, RateConfig] = {
    16000: RateConfig(
        rate=16000,
        mbe_params="""Nfft=1024
Freq=16000
FrameLength=360
FrameShift=120
""",
        period=120,
    ),
    22050: RateConfig(
        rate=22050,
        mbe_params="""Nfft=1024
Freq=16000
FrameLength=540
FrameShift=180
""",
        period=180,
    ),
}

VOWELS = [
    "i:", "I", "E", "a", "A:", "0", "O", "V", "U", "u:", "@", "3", "3:",
    "eI", "aI", "OI", "aU", "oU", "A@", "O@", "e@", "i@", "U@",
]
CONSONANTS = [
    "p", "t", "k", "b", "d", "g", "tS", "dZ", "f", "v", "T", "D", "s", "z",
    "S", "Z", "h", "m", "n", "N", "l", "r", "w", "j",
]

# height (1 high .. 4 low), backness (1 front .. 3 back), rounded, long/diphthong
VOWEL_FEATURES = {
    "i:": (1, 1, 0, 1), "I": (1, 1, 0, 0), "E": (2, 1, 0, 0), "a": (4, 1, 0, 1),
    "A:": (4, 3, 0, 1), "0": (3, 3, 1, 1), "O": (3, 2, 1, 0), "V": (3, 2, 0, 0),
    "U": (1, 3, 1, 0), "u:": (1, 3, 1, 1), "@": (2, 2, 0, 0), "3": (2, 2, 0, 1),
    "3:": (2, 2, 0, 1), "eI": (2, 1, 0, 1), "aI": (4, 1, 0, 1),
    "OI": (3, 2, 1, 1), "aU": (4, 3, 1, 1), "oU": (3, 2, 1, 1),
    "A@": (4, 3, 0, 1), "O@": (3, 2, 1, 1), "e@": (2, 1, 0, 1),
    "i@": (1, 1, 0, 1), "U@": (1, 3, 1, 1),
}
# place (1 bilabial .. 8 glottal), manner (1 stop, 2 affricate, 3 fricative,
# 4 nasal, 5 liquid, 6 glide), voiced
CONSONANT_FEATURES = {
    "p": (1, 1, 0), "b": (1, 1, 1), "m": (1, 4, 1),
    "f": (2, 3, 0), "v": (2, 3, 1),
    "T": (3, 3, 0), "D": (3, 3, 1),
    "t": (4, 1, 0), "d": (4, 1, 1), "s": (4, 3, 0), "z": (4, 3, 1),
    "n": (4, 4, 1), "l": (4, 5, 1), "r": (4, 5, 1),
    "S": (5, 3, 0), "Z": (5, 3, 1), "tS": (5, 2, 0), "dZ": (5, 2, 1),
    "j": (6, 6, 1),
    "k": (7, 1, 0), "g": (7, 1, 1), "N": (7, 4, 1),
    "h": (8, 3, 0),
    "w": (1, 6, 1),
}

VOWEL_SET = set(VOWELS)
PHONES = [p for p in VOWELS + CONSONANTS]


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------


def run(cmd: Sequence[str], cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
    result = subprocess.run(
        [str(c) for c in cmd],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(str(c) for c in cmd)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def load_csv(path: Path) -> List[Dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def fnum(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def phone_distance(a: str, b: str) -> float:
    """Rough articulatory distance, used only to pick donor contexts."""
    if a == b:
        return 0.0
    a_vowel = a in VOWEL_SET
    b_vowel = b in VOWEL_SET
    if a_vowel != b_vowel:
        return 12.0
    if a_vowel:
        fa = VOWEL_FEATURES[a]
        fb = VOWEL_FEATURES[b]
        return (
            1.5 * abs(fa[0] - fb[0])
            + 1.0 * abs(fa[1] - fb[1])
            + 1.5 * abs(fa[2] - fb[2])
            + 0.75 * abs(fa[3] - fb[3])
        )
    fa = CONSONANT_FEATURES[a]
    fb = CONSONANT_FEATURES[b]
    distance = 1.6 * abs(fa[0] - fb[0]) + 1.4 * abs(fa[1] - fb[1]) + 2.0 * abs(fa[2] - fb[2])
    if fa[1] == fb[1] == 3 and fa[2] == fb[2] == 0 and abs(fa[0] - fb[0]) <= 1:
        distance -= 0.5
    return distance


def db_level(samples: np.ndarray) -> float:
    if not len(samples):
        return -120.0
    rms = float(np.sqrt(np.mean(np.square(np.asarray(samples, dtype=np.float64)))))
    return 20.0 * math.log10(max(rms, 1e-9))


# --------------------------------------------------------------------------
# source conversion and slicing
# --------------------------------------------------------------------------

_INFO_CACHE: Dict[str, object] = {}


def read_abs(path, rate: int, t0: float, t1: float) -> np.ndarray:
    key = str(path)
    info = _INFO_CACHE.get(key)
    if info is None:
        info = sf.info(key)
        _INFO_CACHE[key] = info
    total = info.frames / float(info.samplerate)
    t0 = max(0.0, min(t0, total))
    t1 = max(t0, min(t1, total))
    start = int(round(t0 * rate))
    frames = max(1, int(round((t1 - t0) * rate)))
    frames = min(frames, info.frames - start)
    data, _ = sf.read(key, start=start, frames=frames, dtype="float32", always_2d=False)
    return np.asarray(data, dtype=np.float32)


def xfade(left: np.ndarray, right: np.ndarray, n: int) -> np.ndarray:
    t = np.linspace(0.0, 1.0, n, endpoint=False, dtype=np.float32)
    return left * np.cos(t * (np.pi / 2)) + right * np.sin(t * (np.pi / 2))


def prepare_sources(rate: int, work_dir: Path, segments: Dict[int, Dict[str, str]]) -> Dict[str, Path]:
    """Decode each original recording once, mono at the target rate."""
    conv_dir = work_dir / "conv"
    conv_dir.mkdir(parents=True, exist_ok=True)
    mapping: Dict[str, Path] = {}
    sources = sorted({row["source_file"] for row in segments.values()})
    for source in sources:
        src_path = VOICE_AUDIO / source
        out_path = conv_dir / f"{Path(source).stem}_{rate}.wav"
        mapping[source] = out_path
        if out_path.exists() and out_path.stat().st_mtime >= src_path.stat().st_mtime:
            continue
        print(f"  converting {source} -> {out_path.name}")
        run([
            "ffmpeg", "-nostdin", "-v", "error", "-i", str(src_path),
            "-ac", "1", "-ar", str(rate),
            "-af", "aresample=resampler=soxr:precision=28",
            "-c:a", "pcm_s16le", "-y", str(out_path),
        ])
    return mapping


# --------------------------------------------------------------------------
# unit selection
# --------------------------------------------------------------------------


def annotate_rows(rows: List[Dict[str, str]], verdicts: Dict[Tuple[int, str], str]) -> None:
    snr = [fnum(r["snr_db"]) for r in rows]
    ll = [fnum(r["overall_log_likelihood"]) for r in rows]
    snr_mu, snr_sd = statistics.mean(snr), statistics.pstdev(snr) or 1.0
    ll_mu, ll_sd = statistics.mean(ll), statistics.pstdev(ll) or 1.0
    median_rms = statistics.median(fnum(r["rms_db"]) for r in rows)
    for row in rows:
        row["verdict"] = verdicts.get((int(row["line"]), row["diphone"]), "OK")
        score = 1.0 * ((fnum(row["snr_db"]) - snr_mu) / snr_sd)
        score += 0.5 * ((fnum(row["overall_log_likelihood"]) - ll_mu) / ll_sd)
        if row["verdict"] != "OK":
            score -= 2.0
        if min(fnum(row["phone1_duration_s"]), fnum(row["phone2_duration_s"])) < 0.035:
            score -= 1.0
        if fnum(row["peak"]) > 0.9:
            score -= 3.0
        if fnum(row["f0_std_hz"], 99.0) > 30.0:
            score -= 0.5
        if abs(fnum(row["rms_db"]) - median_rms) > 8.0:
            score -= 1.0
        row["score"] = round(score, 4)


def select_takes(rows: List[Dict[str, str]]) -> Tuple[Dict[str, Dict[str, str]], List[Dict[str, object]]]:
    by_label: Dict[str, List[Dict[str, str]]] = collections.defaultdict(list)
    for row in rows:
        by_label[row["diphone"]].append(row)
    chosen: Dict[str, Dict[str, str]] = {}
    report: List[Dict[str, object]] = []
    for label, takes in sorted(by_label.items()):
        winner = max(takes, key=lambda r: (float(r["score"]), -int(r["line"])))
        chosen[label] = winner
        report.append({
            "diphone": label,
            "line": int(winner["line"]),
            "takes": len(takes),
            "score": float(winner["score"]),
            "snr_db": fnum(winner["snr_db"]),
            "log_likelihood": fnum(winner["overall_log_likelihood"]),
            "verdict": winner["verdict"],
            "source_wav": winner["source_wav"],
        })
    return chosen, report


def natural_spec(label: str, row: Dict[str, str], segment: Dict[str, str]) -> Dict[str, object]:
    p1_start = fnum(row["phone1_start_s"])
    p1_end = fnum(row["phone1_end_s"])
    p2_start = fnum(row["phone2_start_s"])
    p2_end = fnum(row["phone2_end_s"])
    middle = p1_end if abs(p1_end - p2_start) < 1e-4 else p2_start
    return {
        "label": label,
        "kind": "natural",
        "left": row["phone1"],
        "right": row["phone2"],
        "line": int(row["line"]),
        "source_file": segment["source_file"],
        "source_start": fnum(segment["source_start_s"]),
        "begin": p1_start + (p1_end - p1_start) / 2.0,
        "middle": middle,
        "end": p2_start + (p2_end - p2_start) / 2.0,
        "note": row["verdict"].lower(),
    }


# --------------------------------------------------------------------------
# word-edge units
# --------------------------------------------------------------------------


def edge_specs(
    rows: List[Dict[str, str]],
    segments: Dict[int, Dict[str, str]],
    sources: Dict[str, Path],
    rate: int,
) -> List[Dict[str, object]]:
    initial: Dict[str, List[Dict[str, str]]] = collections.defaultdict(list)
    final: Dict[str, List[Dict[str, str]]] = collections.defaultdict(list)
    for row in rows:
        if abs(fnum(row["phone1_start_s"]) - fnum(row["word_start_s"])) < 1e-4:
            initial[row["phone1"]].append(row)
        if abs(fnum(row["phone2_end_s"]) - fnum(row["word_end_s"])) < 1e-4:
            final[row["phone2"]].append(row)

    def tone_db(row: Dict[str, str], side: str) -> float:
        segment = segments[int(row["line"])]
        path = sources[segment["source_file"]]
        start = fnum(segment["source_start_s"])
        if side == "pre":
            t0 = start + fnum(row["word_start_s"]) - SILENCE_HALF
            t1 = start + fnum(row["word_start_s"])
        else:
            t0 = start + fnum(row["word_end_s"])
            t1 = t0 + SILENCE_HALF
        return db_level(read_abs(path, rate, t0, t1))

    specs: List[Dict[str, object]] = []
    for phone in PHONES:
        # _phone: real room tone + the first half of the phone.
        best = None
        for row in initial.get(phone, []):
            tone = tone_db(row, "pre")
            span = fnum(row["phone1_duration_s"]) / 2.0
            key = (tone <= -40.0, -tone, float(row["score"]) + span * 10.0)
            if best is None or key > best[0]:
                best = (key, row)
        if best is not None and best[0][0]:
            row = best[1]
            segment = segments[int(row["line"])]
            word_edge = fnum(row["word_start_s"])
            phone_start = fnum(row["phone1_start_s"])
            phone_end = fnum(row["phone1_end_s"])
            specs.append({
                "label": f"_{phone}",
                "kind": "initial",
                "left": "_",
                "right": phone,
                "line": int(row["line"]),
                "source_file": segment["source_file"],
                "source_start": fnum(segment["source_start_s"]),
                "begin": word_edge - SILENCE_HALF,
                "middle": word_edge,
                "end": phone_start + (phone_end - phone_start) / 2.0,
                "note": "recorded room tone",
            })
        else:
            specs.append(splice_edge(phone, "initial", rows, segments))

        # phone_: the second half of the phone + real room tone.
        best = None
        for row in final.get(phone, []):
            tone = tone_db(row, "post")
            span = fnum(row["phone2_duration_s"]) / 2.0
            key = (tone <= -40.0, -tone, float(row["score"]) + span * 10.0)
            if best is None or key > best[0]:
                best = (key, row)
        if best is not None and best[0][0]:
            row = best[1]
            segment = segments[int(row["line"])]
            word_edge = fnum(row["word_end_s"])
            phone_start = fnum(row["phone2_start_s"])
            phone_end = fnum(row["phone2_end_s"])
            specs.append({
                "label": f"{phone}_",
                "kind": "final",
                "left": phone,
                "right": "_",
                "line": int(row["line"]),
                "source_file": segment["source_file"],
                "source_start": fnum(segment["source_start_s"]),
                "begin": phone_start + (phone_end - phone_start) / 2.0,
                "middle": word_edge,
                "end": word_edge + SILENCE_HALF,
                "note": "recorded room tone",
            })
        else:
            specs.append(splice_edge(phone, "final", rows, segments))
    return specs


def splice_edge(phone: str, side: str, rows: List[Dict[str, str]], segments: Dict[int, Dict[str, str]]) -> Dict[str, object]:
    """Word-edge unit for a phone that cannot occur at that word edge."""
    candidates = [r for r in rows if r["phone1"] == phone or r["phone2"] == phone]
    candidates.sort(key=lambda r: (-float(r["score"]), int(r["line"])))
    if side == "initial":
        row = next(r for r in candidates if r["phone1"] == phone)
        segment = segments[int(row["line"])]
        start = fnum(segment["source_start_s"])
        p1 = fnum(row["phone1_start_s"])
        mid = p1 + (fnum(row["phone1_end_s"]) - p1) / 2.0
        return {
            "label": f"_{phone}",
            "kind": "initial",
            "left": "_",
            "right": phone,
            "line": int(row["line"]),
            "source_file": segment["source_file"],
            "source_start": start,
            "begin": fnum(row["word_start_s"]) - SILENCE_HALF,
            "middle": fnum(row["word_start_s"]),
            "end": mid,
            "note": "spliced room tone + first half",
            "splice": {
                "mode": "tone_then_half",
                "source": segment["source_file"],
                "half_start": start + p1,
                "half_end": start + mid,
                "tone_start": start + fnum(row["word_start_s"]) - TONE_LENGTH,
                "tone_end": start + fnum(row["word_start_s"]),
            },
        }
    # No recording has this phone at a word end, so use its second half from a
    # word-initial take and append the room tone that follows that word.
    row = next(r for r in candidates if r["phone1"] == phone)
    segment = segments[int(row["line"])]
    start = fnum(segment["source_start_s"])
    p1 = fnum(row["phone1_start_s"])
    mid = p1 + (fnum(row["phone1_end_s"]) - p1) / 2.0
    return {
        "label": f"{phone}_",
        "kind": "final",
        "left": phone,
        "right": "_",
        "line": int(row["line"]),
        "source_file": segment["source_file"],
        "source_start": start,
        "begin": mid,
        "middle": fnum(row["word_end_s"]),
        "end": fnum(row["word_end_s"]) + SILENCE_HALF,
        "note": "spliced second half + room tone",
        "splice": {
            "mode": "half_then_tone",
            "source": segment["source_file"],
            "half_start": start + mid,
            "half_end": start + fnum(row["phone1_end_s"]),
            "tone_start": start + fnum(row["word_end_s"]),
            "tone_end": start + fnum(row["word_end_s"]) + TONE_LENGTH,
        },
    }


# --------------------------------------------------------------------------
# spliced fallback pairs
# --------------------------------------------------------------------------


def fallback_specs(
    rows: List[Dict[str, str]],
    segments: Dict[int, Dict[str, str]],
    chosen: Dict[str, Dict[str, str]],
) -> List[Dict[str, object]]:
    by_left: Dict[str, List[Dict[str, object]]] = collections.defaultdict(list)
    by_right: Dict[str, List[Dict[str, object]]] = collections.defaultdict(list)
    for label, row in chosen.items():
        segment = segments[int(row["line"])]
        start = fnum(segment["source_start_s"])
        p1 = fnum(row["phone1_start_s"])
        p1_end = fnum(row["phone1_end_s"])
        p2 = fnum(row["phone2_start_s"])
        p2_end = fnum(row["phone2_end_s"])
        by_left[row["phone1"]].append({
            "row": row,
            "source_file": segment["source_file"],
            "score": float(row["score"]),
            "mid": start + p1 + (p1_end - p1) / 2.0,
            "edge": start + p1_end,
            "other": row["phone2"],
        })
        by_right[row["phone2"]].append({
            "row": row,
            "source_file": segment["source_file"],
            "score": float(row["score"]),
            "edge": start + p2,
            "start": start + p2,
            "mid": start + p2 + (p2_end - p2) / 2.0,
            "other": row["phone1"],
        })

    word_initial: Dict[str, List[Dict[str, object]]] = collections.defaultdict(list)
    for row in rows:
        if abs(fnum(row["phone1_start_s"]) - fnum(row["word_start_s"])) < 1e-4:
            segment = segments[int(row["line"])]
            start = fnum(segment["source_start_s"])
            p1 = fnum(row["phone1_start_s"])
            word_initial[row["phone1"]].append({
                "row": row,
                "source_file": segment["source_file"],
                "score": float(row["score"]),
                "start": start + fnum(row["word_start_s"]),
                "mid": start + p1 + (fnum(row["phone1_end_s"]) - p1) / 2.0,
                "other": row["phone1"],
            })

    specs: List[Dict[str, object]] = []
    for a in PHONES:
        for b in PHONES:
            label = f"{a}_{b}"
            if label in chosen:
                continue
            left_candidates = by_left.get(a, [])
            if not left_candidates:
                raise RuntimeError(f"{label}: no left-half donor for {a}")
            left = min(left_candidates, key=lambda d: (phone_distance(d["other"], b), -d["score"]))

            right_mode = "right"
            right_candidates = by_right.get(b, [])
            if right_candidates:
                right = min(right_candidates, key=lambda d: (phone_distance(d["other"], a), -d["score"]))
            else:
                initial_candidates = word_initial.get(b, [])
                if not initial_candidates:
                    raise RuntimeError(f"{label}: no first-half donor for {b}")
                right = max(initial_candidates, key=lambda d: d["score"])
                right_mode = "initial"

            specs.append({
                "label": label,
                "kind": "fallback",
                "left": a,
                "right": b,
                "line": int(left["row"]["line"]),
                "source_file": left["source_file"],
                "note": (
                    f"left half from {left['other']}_{a}, right half from "
                    + (f"{b}_{b} (word start)" if right_mode == "initial" else f"{right['other']}_{b}")
                ),
                "fallback": {
                    "left_source": left["source_file"],
                    "left_mid": left["mid"],
                    "left_edge": left["edge"],
                    "left_context": left["other"],
                    "right_source": right["source_file"],
                    "right_start": right["start"],
                    "right_mid": right["mid"],
                    "right_mode": right_mode,
                    "right_context": right["other"],
                },
            })
    return specs


# --------------------------------------------------------------------------
# audio assembly
# --------------------------------------------------------------------------


def assemble(spec: Dict[str, object], rate: int, sources: Dict[str, Path]) -> Tuple[np.ndarray, int, int, int]:
    if spec["kind"] in ("natural", "initial", "final") and "splice" not in spec:
        path = sources[spec["source_file"]]
        start = float(spec["source_start"])
        begin, middle, end = float(spec["begin"]), float(spec["middle"]), float(spec["end"])
        total = sf.info(str(path)).frames / float(rate)
        t0 = max(0.0, start + begin - PAD)
        t1 = min(total, start + end + PAD)
        audio = read_abs(path, rate, t0, t1)
        first = int(round((start + begin - t0) * rate))
        mid_i = int(round((start + middle - t0) * rate))
        last = int(round((start + end - t0) * rate))
        if first < 0.05 * rate or len(audio) - last < 0.05 * rate:
            print(f"  warning: {spec['label']}: thin analysis margin")
        if not (0 <= first < mid_i < last <= len(audio)):
            raise RuntimeError(f"{spec['label']}: bad indexes {first} {mid_i} {last} of {len(audio)}")
        return audio, first, mid_i, last

    if "splice" in spec:
        splice = spec["splice"]
        path = sources[splice["source"]]
        half_start = float(splice["half_start"])
        half_end = float(splice["half_end"])
        tone = read_abs(path, rate, float(splice["tone_start"]), float(splice["tone_end"]))
        if splice["mode"] == "tone_then_half":
            half = read_abs(path, rate, half_start, half_end + PAD)
            n = min(int(XFADE * rate), max(1, len(tone) // 3), max(1, len(half) // 3))
            audio = np.concatenate([tone[:-n], xfade(tone[-n:], half[:n], n), half[n:]])
            middle = len(tone) - n + n // 2
            first = middle - int(round(SILENCE_HALF * rate))
            last = len(tone) - n + int(round((half_end - half_start) * rate))
        else:
            half = read_abs(path, rate, half_start - PAD, half_end)
            n = min(int(XFADE * rate), max(1, len(half) // 3), max(1, len(tone) // 3))
            audio = np.concatenate([half[:-n], xfade(half[-n:], tone[:n], n), tone[n:]])
            middle = len(half) - n + n // 2
            first = int(round(PAD * rate))
            last = middle + int(round(SILENCE_HALF * rate))
        if not (0 <= first < middle < last <= len(audio)):
            raise RuntimeError(f"{spec['label']}: bad splice indexes {first} {middle} {last} of {len(audio)}")
        return audio, first, middle, last

    fb = spec["fallback"]
    left_mid = float(fb["left_mid"])
    left_t0 = max(0.0, left_mid - PAD)
    left = read_abs(sources[fb["left_source"]], rate, left_t0, float(fb["left_edge"]))
    right = read_abs(
        sources[fb["right_source"]], rate,
        float(fb["right_start"]), float(fb["right_mid"]) + PAD,
    )
    n = min(int(XFADE * rate), max(1, len(left) // 3), max(1, len(right) // 3))
    if n >= 8:
        audio = np.concatenate([left[:-n], xfade(left[-n:], right[:n], n), right[n:]])
        middle = len(left) - n + n // 2
    else:
        audio = np.concatenate([left, right])
        middle = len(left)
    first = int(round((left_mid - left_t0) * rate))
    right_mid_index = int(round((float(fb["right_mid"]) - float(fb["right_start"])) * rate))
    last = len(left) + right_mid_index - (n if n >= 8 else 0)
    if not (0 <= first < middle < last <= len(audio)):
        raise RuntimeError(f"{spec['label']}: bad fallback indexes {first} {middle} {last} of {len(audio)}")
    return audio, first, middle, last


# --------------------------------------------------------------------------
# analysis workers
# --------------------------------------------------------------------------

_WORKER: Dict[str, object] = {}


def init_worker(rate: int, sources: Dict[str, str], build_dir: str, name: str, config: RateConfig,
                max_half: int = 0) -> None:
    _WORKER.clear()
    _WORKER.update({"rate": rate, "sources": sources, "build_dir": Path(build_dir), "name": name,
                    "config": config, "max_half": max_half})
    _INFO_CACHE.clear()


def process_unit(job: Tuple[int, Dict[str, object]]) -> Dict[str, object]:
    index, spec = job
    rate = int(_WORKER["rate"])
    sources = _WORKER["sources"]
    build_dir = _WORKER["build_dir"]
    name = _WORKER["name"]

    audio, first, middle, last = assemble(spec, rate, sources)
    max_half = int(_WORKER.get("max_half", 0))
    if max_half > 0:
        # Keep the junction and the phone identity, drop outer steady state.
        # MBROLA stretches each half to the phone length it is given, so this
        # only shortens the stored unit, not the synthesized durations.
        first = max(first, middle - max_half)
        last = min(last, middle + max_half)
    raw_path = build_dir / "raw" / f"{index:04d}.d16"
    pcm = np.clip(audio, -1.0, 1.0) * 32767.0
    pcm.astype("<i2").tofile(str(raw_path))

    f0_path = build_dir / "ana" / f"{index}.f0"
    mbe_path = build_dir / "ana" / f"{index}.mbe"
    out_prefix = build_dir / "res" / str(index)
    run([ANAF0, raw_path, f0_path, first, last, build_dir / f"{name}.f0"])
    run([ANAMBE, raw_path, f0_path, mbe_path, first, last, build_dir / f"{name}.mbe"])
    run([RESYNTH, raw_path, mbe_path, out_prefix, build_dir / f"{name}.syn"])

    s16_path = Path(str(out_prefix) + ".s16")
    pm_path = Path(str(out_prefix) + ".pm")
    for path in (s16_path, pm_path):
        if not path.exists() or path.stat().st_size == 0:
            raise RuntimeError(f"{spec['label']}: resynth did not create {path}")
    return {
        "index": index,
        "label": spec["label"],
        "left": spec["left"],
        "right": spec["right"],
        "kind": spec["kind"],
        "line": spec["line"],
        "source_file": spec["source_file"],
        "note": spec.get("note", ""),
        "first": first,
        "middle": middle,
        "last": last,
        "span_samples": last - first,
        "s16": str(s16_path),
    }


def pad_resynth_output(s16_path: Path, period: int) -> int:
    size = s16_path.stat().st_size
    if size % 2:
        with s16_path.open("r+b") as handle:
            handle.truncate(size - 1)
    samples = s16_path.stat().st_size // 2
    remainder = samples % period
    if remainder == 0:
        return 0
    padding = (period - remainder) * 2
    with s16_path.open("ab") as handle:
        handle.write(b"\x00" * padding)
    return padding // 2


def peak_of(path: Path) -> int:
    data = array.array("h")
    data.frombytes(path.read_bytes())
    return max((abs(v) for v in data), default=0)


def scale_outputs(paths: Iterable[Path], gain: float) -> None:
    for path in paths:
        data = array.array("h")
        data.frombytes(path.read_bytes())
        path.write_bytes(array.array(
            "h", (max(-32768, min(32767, int(round(v * gain)))) for v in data)
        ).tobytes())


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------


def write_pho(path: Path, phones: Sequence[str]) -> None:
    lines = []
    for phone in phones:
        if phone == "_":
            lines.append("_ 60")
        elif phone in VOWEL_SET:
            lines.append(f"{phone} 190 50 120")
        else:
            lines.append(f"{phone} 90")
    path.write_text("\n".join(lines) + "\n", encoding="ascii")


def wav_stats(path: Path) -> Dict[str, float]:
    data, rate = sf.read(str(path), dtype="int16")
    if data.ndim > 1:
        data = data[:, 0]
    data = np.asarray(data, dtype=np.int32)
    rms = float(np.sqrt(np.mean(np.square(data.astype(np.float64))))) if len(data) else 0.0
    peak = float(np.max(np.abs(data))) if len(data) else 0.0
    quiet = np.abs(data) < 8
    longest = 0
    if quiet.any():
        edges = np.flatnonzero(np.diff(np.concatenate(([0], quiet.view(np.int8), [0]))))
        runs = edges[1::2] - edges[::2]
        longest = int(runs.max()) if len(runs) else 0
    return {
        "rate": int(rate),
        "duration_s": len(data) / rate,
        "rms": rms,
        "peak": peak,
        "longest_silence_s": longest / rate,
        "zeros_pct": 100.0 * float(np.mean(data == 0)) if len(data) else 0.0,
    }


TEST_WORDS = {
    "see": ["s", "i:"],
    "tap": ["t", "a", "p"],
    "pat": ["p", "a", "t"],
    "extras": ["E", "k", "s", "t", "r", "@", "z"],
    "further": ["f", "3:", "D", "3", "r"],
    "computer": ["k", "@", "m", "p", "j", "u:", "t", "3", "r"],
    "unnamed": ["V", "n", "n", "eI", "m", "d"],
    "bookkeeper": ["b", "U", "k", "k", "i:", "p", "3", "r"],
    "output": ["aU", "t", "p", "U", "t"],
    "texts": ["t", "E", "k", "s", "t", "s"],
    "asked": ["A:", "s", "k", "t"],
    "strengths": ["s", "t", "r", "E", "N", "T", "s"],
}


def validate(db_path: Path, manifest: List[Dict[str, object]], out_dir: Path) -> List[Dict[str, object]]:
    test_dir = out_dir / "tests"
    test_dir.mkdir(parents=True, exist_ok=True)
    results: List[Dict[str, object]] = []
    labels = [str(unit["label"]) for unit in manifest]

    chunk_size = 300
    for chunk_index in range(0, len(labels), chunk_size):
        phones: List[str] = []
        for label in labels[chunk_index:chunk_index + chunk_size]:
            if label.startswith("_"):
                left, right = "_", label[1:]
            elif label.endswith("_"):
                left, right = label[:-1], "_"
            else:
                left, right = label.split("_", 1)
            phones.extend(["_", left, right])
        phones.append("_")
        pho = test_dir / f"coverage_{chunk_index // chunk_size:02d}.pho"
        write_pho(pho, phones)
        wav = pho.with_suffix(".wav")
        result = run([MBROLA, db_path, pho, wav])
        stats = wav_stats(wav)
        results.append({"test": pho.stem, "status": "ok", **stats, "log": result.stderr.strip()})
        print(f"  {pho.name}: {stats['duration_s']:.1f}s rms={stats['rms']:.0f} "
              f"peak={stats['peak']:.0f} silence={stats['longest_silence_s']:.2f}s")

    for name, phones in TEST_WORDS.items():
        pho = test_dir / f"word_{name}.pho"
        write_pho(pho, ["_"] + phones + ["_"])
        wav = pho.with_suffix(".wav")
        run([MBROLA, db_path, pho, wav])
        stats = wav_stats(wav)
        results.append({"test": pho.stem, "status": "ok", **stats})
        print(f"  {pho.name}: {stats['duration_s']:.2f}s rms={stats['rms']:.0f} "
              f"peak={stats['peak']:.0f} silence={stats['longest_silence_s']:.2f}s")

    (out_dir / "validation.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    return results


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rate", type=int, default=22050, choices=sorted(RATE_CONFIGS))
    parser.add_argument("--out", default=str(PROJECT_ROOT / "output" / "mbrola_db"))
    parser.add_argument("--name", default="usd")
    parser.add_argument("--jobs", type=int, default=max(1, min(8, (os.cpu_count() or 4) - 1)))
    parser.add_argument("--limit", type=int, default=0, help="build only the first N units (pilot)")
    parser.add_argument("--pilot", action="store_true", help="build a small representative subset")
    parser.add_argument("--max-half-ms", type=float, default=0.0,
                        help="keep at most this many ms of each half around the phone "
                             "junction (0 keeps the full span). Shorter units make a "
                             "smaller database; MBROLA stretches them to the requested "
                             "phone durations.")
    parser.add_argument("--keep", action="store_true", help="do not clear an existing build directory")
    parser.add_argument("--no-validate", action="store_true")
    args = parser.parse_args()

    for tool in (ANAF0, ANAMBE, RESYNTH, DATABASE_BUILD, MBROLA):
        if not tool.exists():
            print(f"missing tool: {tool}", file=sys.stderr)
            return 1
    config = RATE_CONFIGS[args.rate]

    out_root = Path(args.out)
    build_dir = out_root / args.name
    if build_dir.exists() and not args.keep:
        shutil.rmtree(build_dir)
    for sub in ("raw", "ana", "res"):
        (build_dir / sub).mkdir(parents=True, exist_ok=True)
    out_root.mkdir(parents=True, exist_ok=True)

    (build_dir / f"{args.name}.f0").write_text(F0_PARAMS, encoding="ascii")
    (build_dir / f"{args.name}.mbe").write_text(config.mbe_params, encoding="ascii")
    (build_dir / f"{args.name}.syn").write_text(SYN_PARAMS, encoding="ascii")

    start_all = time.perf_counter()
    rows = load_csv(UNIT_GRID)
    segments = {int(row["script_line"]): row for row in load_csv(SEGMENTS)}
    verdicts: Dict[Tuple[int, str], str] = {}
    if UNIT_AUDIT.exists():
        for row in load_csv(UNIT_AUDIT):
            verdicts[(int(row["line"]), row["diphone"])] = row["verdict"]

    print("selecting takes")
    annotate_rows(rows, verdicts)
    chosen, selection_report = select_takes(rows)
    with (out_root / "take_selection.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(selection_report[0].keys()))
        writer.writeheader()
        writer.writerows(selection_report)
    print(f"  {len(chosen)} recorded labels from {len(rows)} takes")

    print("converting source audio")
    sources = prepare_sources(args.rate, build_dir, segments)

    print("building word-edge units")
    edges = edge_specs(rows, segments, sources, args.rate)
    print(f"  {len(edges)} edge units")

    naturals = [natural_spec(label, row, segments[int(row["line"])]) for label, row in sorted(chosen.items())]
    print("building spliced fallback units")
    fallbacks = fallback_specs(rows, segments, chosen)
    print(f"  {len(fallbacks)} spliced pairs")

    specs = naturals + edges + fallbacks
    if args.pilot:
        picked = naturals[:8] + edges + fallbacks[:16]
        specs = picked
    if args.limit:
        specs = specs[:args.limit]
    print(f"total units: {len(specs)}")

    jobs = [(index, spec) for index, spec in enumerate(specs)]
    print(f"assembling and analyzing units with {args.jobs} workers")
    results: List[Dict[str, object]] = []
    failures: List[str] = []
    processed = 0
    with ProcessPoolExecutor(
        max_workers=args.jobs,
        initializer=init_worker,
        initargs=(args.rate, {k: str(v) for k, v in sources.items()}, str(build_dir), args.name, config,
                  int(round(args.max_half_ms * args.rate / 1000.0))),
    ) as pool:
        futures = {pool.submit(process_unit, job): job for job in jobs}
        for future in as_completed(futures):
            job = futures[future]
            try:
                results.append(future.result())
            except Exception as error:  # noqa: BLE001
                failures.append(f"{job[1]['label']}: {error}")
                print(f"  FAILED {job[1]['label']}: {error}")
            processed += 1
            if processed % 100 == 0 or processed == len(jobs):
                print(f"  {processed}/{len(jobs)} units")
    results.sort(key=lambda item: int(item["index"]))

    if failures:
        (out_root / "build_failures.txt").write_text("\n".join(failures) + "\n", encoding="utf-8")
        print(f"{len(failures)} units failed; see build_failures.txt")
    if not results:
        print("no units built", file=sys.stderr)
        return 1

    print("padding and scaling units")
    for item in results:
        pad_resynth_output(Path(str(item["s16"])), config.period)
    max_peak = max(peak_of(Path(str(item["s16"]))) for item in results)
    gain = RESYNTH_GAIN if max_peak == 0 else min(RESYNTH_GAIN, MAX_PEAK_FRACTION * 32767.0 / max_peak)
    scale_outputs((Path(str(item["s16"])) for item in results), gain)
    print(f"  gain {gain:.3f} (peak before gain {max_peak})")

    unit_stats: List[Dict[str, object]] = []
    for item in results:
        data = np.fromfile(str(item["s16"]), dtype="<i2")
        rms = float(np.sqrt(np.mean(np.square(data.astype(np.float64))))) if len(data) else 0.0
        unit_stats.append({
            "diphone": item["label"],
            "kind": item["kind"],
            "line": item["line"],
            "span_ms": round(float(item["span_samples"]) / args.rate * 1000.0, 1),
            "out_ms": round(len(data) / args.rate * 1000.0, 1),
            "rms_db": round(20.0 * math.log10(max(rms / 32768.0, 1e-9)), 1),
            "peak": int(np.max(np.abs(data))) if len(data) else 0,
        })
    with (out_root / "unit_stats.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(unit_stats[0].keys()))
        writer.writeheader()
        writer.writerows(unit_stats)
    quiet = sorted(unit_stats, key=lambda row: float(row["rms_db"]))[:8]
    print("  quietest units: " + ", ".join(f"{r['diphone']}({r['rms_db']} dB)" for r in quiet))

    manifest: List[Dict[str, object]] = []
    seg_lines: List[str] = []
    for item in results:
        spec = specs[int(item["index"])]
        seg_lines.append(
            f"raw/{int(item['index']):04d}.d16\t{spec['left']}\t{spec['right']}\t"
            f"{item['first']}\t{item['last']}\t{item['middle']}\n"
        )
        manifest.append({
            **{k: v for k, v in item.items() if k != "s16"},
            "fallback": spec.get("fallback"),
            "splice": spec.get("splice"),
        })
    (build_dir / f"{args.name}.seg").write_text("".join(seg_lines), encoding="ascii")
    (build_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print("packing database")
    pack_start = time.perf_counter()
    result = run(
        [DATABASE_BUILD, args.name, "res/", "res/", DATABASE_VERSION, args.rate,
         config.period, DATABASE_CODING, DATABASE_VOLUME],
        cwd=build_dir,
    )
    (build_dir / "database_build.log").write_text(result.stdout + result.stderr, encoding="utf-8")
    db_path = build_dir / args.name
    if not db_path.exists() or db_path.stat().st_size == 0:
        print("database_build did not create the database", file=sys.stderr)
        return 1
    print(f"  database {db_path} ({db_path.stat().st_size / 1e6:.1f} MB) in {time.perf_counter() - pack_start:.1f}s")

    if not args.no_validate:
        print("validating")
        try:
            validate(db_path, manifest, out_root)
        except Exception as error:  # noqa: BLE001
            print(f"validation failed: {error}", file=sys.stderr)
            (out_root / "validation_error.txt").write_text(str(error), encoding="utf-8")

    fallback_rows = [
        {"diphone": spec["label"],
         "left_context": spec["fallback"]["left_context"],
         "right_context": spec["fallback"]["right_context"]}
        for spec in specs if spec["kind"] == "fallback"
    ]
    with (out_root / "fallback_pairs.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["diphone", "left_context", "right_context"])
        writer.writeheader()
        writer.writerows(fallback_rows)

    summary = {
        "database": str(db_path),
        "name": args.name,
        "rate": args.rate,
        "units": len(results),
        "natural": sum(1 for s in specs if s["kind"] == "natural"),
        "edge": sum(1 for s in specs if s["kind"] in ("initial", "final")),
        "fallback": sum(1 for s in specs if s["kind"] == "fallback"),
        "failures": len(failures),
        "db_bytes": db_path.stat().st_size,
        "gain": gain,
        "elapsed_s": round(time.perf_counter() - start_all, 1),
    }
    (out_root / "build_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0 if not failures else 2


if __name__ == "__main__":
    sys.exit(main())
