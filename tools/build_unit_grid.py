#!/usr/bin/env python3
"""
Build the diphone unit grid from MFA-aligned TextGrids.

For every line in the recording script this tool:
  1. Reads the forced-alignment TextGrid.
  2. Locates the target word interval (e.g. ``cv_p_ii``).
  3. Matches the ARPAbet phone intervals inside that word to the eSpeak phones
     listed in ``diphone_map.json``.
  4. Extracts a diphone window from the midpoint of phone 1 to the midpoint of
     phone 2 (the classic diphone definition).
  5. Estimates F0, energy, voicing, and other diagnostics.
  6. Writes extracted unit WAVs + metadata and a single unit-grid CSV/JSON.

Usage:
    python tools/build_unit_grid.py
    python tools/build_unit_grid.py --output-dir output
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import librosa
import numpy as np
import scipy.signal
import soundfile as sf


# ---------------------------------------------------------------------------
# Phoneme classes for extraction tuning
# ---------------------------------------------------------------------------


def load_phoneme_classes(path: Path) -> Dict[str, set]:
    data = json.loads(path.read_text(encoding="utf-8"))
    stops = {"p", "t", "k", "b", "d", "g"}
    affricates = {"tS", "dZ"}
    vowels = set(data.get("vowels", []))
    consonants = set(data.get("consonants", []))
    diphthongs = {"eI", "aI", "OI", "aU", "oU"}
    rhotic = {"A@", "O@", "e@", "i@", "U@"}
    long_vowels = vowels - diphthongs - rhotic
    obstruents = stops | affricates | {"f", "v", "T", "D", "s", "z", "S", "Z", "h"}
    return {
        "stops": stops,
        "affricates": affricates,
        "vowels": vowels,
        "consonants": consonants,
        "diphthongs": diphthongs,
        "rhotic": rhotic,
        "long_vowels": long_vowels,
        "obstruents": obstruents,
    }


PHONEME_CLASSES: Optional[Dict[str, set]] = None


def phoneme_class(name: str) -> Dict[str, set]:
    global PHONEME_CLASSES
    if PHONEME_CLASSES is None:
        PHONEME_CLASSES = load_phoneme_classes(Path("data") / "phonemes.json")
    return PHONEME_CLASSES


# ---------------------------------------------------------------------------
# TextGrid parsing (short "ooTextFile" format only)
# ---------------------------------------------------------------------------


def parse_textgrid(path: Path) -> Dict[str, List[Dict[str, object]]]:
    """Return a dict mapping tier name -> list of intervals."""
    text = path.read_text(encoding="utf-8")
    lines = [ln.strip() for ln in text.splitlines()]

    # Remove the header lines
    while lines and not lines[0].startswith("size"):
        lines.pop(0)
    if not lines:
        raise ValueError(f"No tier data found in {path}")

    tiers: Dict[str, List[Dict[str, object]]] = {}
    current_tier: Optional[str] = None
    intervals: Optional[List[Dict[str, object]]] = None
    pending: Dict[str, object] = {}

    i = 0
    while i < len(lines):
        line = lines[i]

        # Start of a new tier block
        m = re.match(r"item\s*\[(\d+)\]:", line)
        if m:
            if current_tier and intervals is not None:
                if pending and "text" in pending:
                    intervals.append(pending)
                tiers[current_tier] = intervals
            current_tier = None
            intervals = []
            pending = {}
            i += 1
            continue

        # Tier name
        m = re.match(r'name\s*=\s*"([^"]*)"', line)
        if m:
            current_tier = m.group(1)
            pending = {}  # discard tier-level xmin/xmax that preceded the name
            i += 1
            continue

        # Interval block start
        m = re.match(r"intervals\s*\[(\d+)\]:", line)
        if m:
            if pending and "text" in pending:
                intervals.append(pending)
            pending = {}
            i += 1
            continue

        # xmin / xmax / text
        m = re.match(r'xmin\s*=\s*([\d.eE+-]+)', line)
        if m:
            pending["xmin"] = float(m.group(1))
            i += 1
            continue

        m = re.match(r'xmax\s*=\s*([\d.eE+-]+)', line)
        if m:
            pending["xmax"] = float(m.group(1))
            i += 1
            continue

        m = re.match(r'text\s*=\s*"(.*)"', line)
        if m:
            pending["text"] = m.group(1).strip()
            i += 1
            continue

        i += 1

    if current_tier and intervals is not None:
        if pending and "text" in pending:
            intervals.append(pending)
        tiers[current_tier] = intervals

    return tiers


# ---------------------------------------------------------------------------
# Dictionary loading / mapping
# ---------------------------------------------------------------------------


def load_dictionary(path: Path) -> Dict[str, List[str]]:
    """Load a tab-separated token -> phones dictionary."""
    result: Dict[str, List[str]] = {}
    with path.open(encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln or ln.startswith("#"):
                continue
            parts = ln.split("\t")
            if len(parts) < 2:
                # Fall back to whitespace if someone regenerated the file
                parts = ln.split(None, 1)
                if len(parts) < 2:
                    continue
            token, phones = parts[0], parts[1].split()
            if token not in result:
                result[token] = phones
    return result


RHOTIC_VOWELS = {"A@", "O@", "e@", "i@", "U@"}


def align_espeak_to_arpa(
    espeak_phones: List[str], arpa_phones: List[str]
) -> Dict[str, Tuple[int, ...]]:
    """
    Map each eSpeak phone to the indices of the ARPAbet phone interval(s) it
    corresponds to in the MFA TextGrid.

    Most eSpeak phones map 1:1. R-colored vowels (A@, O@, e@, i@, U@) map to
    two ARPAbet symbols (e.g. AA1 R).
    """
    mapping: Dict[str, Tuple[int, ...]] = {}
    arpa_idx = 0
    for esp in espeak_phones:
        if esp in RHOTIC_VOWELS:
            if arpa_idx + 1 >= len(arpa_phones):
                raise ValueError(
                    f"R-colored vowel {esp!r} needs two ARPAbet phones, "
                    f"only {arpa_phones[arpa_idx:]} left"
                )
            mapping[esp] = (arpa_idx, arpa_idx + 1)
            arpa_idx += 2
        else:
            if arpa_idx >= len(arpa_phones):
                raise ValueError(
                    f"eSpeak phone {esp!r} has no matching ARPAbet phone"
                )
            mapping[esp] = (arpa_idx,)
            arpa_idx += 1
    if arpa_idx != len(arpa_phones):
        raise ValueError(
            f"Left-over ARPAbet phones after alignment: {arpa_phones[arpa_idx:]!r}"
        )
    return mapping


# ---------------------------------------------------------------------------
# Unit extraction
# ---------------------------------------------------------------------------


def safe_filename(text: str) -> str:
    """Make a string safe for use in a Windows/Unix file name."""
    return re.sub(r'[^\w\-+=.]', '_', text)


def extract_window(
    audio: np.ndarray,
    sr: int,
    window_start: float,
    window_end: float,
    target_sr: int = 22050,
) -> np.ndarray:
    """Extract a time window from audio and resample to target_sr."""
    start_sample = max(0, int(round(window_start * sr)))
    end_sample = min(len(audio), int(round(window_end * sr)))
    if end_sample <= start_sample:
        return np.array([], dtype=audio.dtype)
    chunk = audio[start_sample:end_sample]
    if sr != target_sr:
        chunk = _resample(chunk, sr, target_sr)
    return chunk


def _resample(chunk: np.ndarray, orig_sr: int, target_sr: int) -> np.ndarray:
    """Resample using scipy (integer-ratio polyphase when possible)."""
    if orig_sr == target_sr:
        return chunk
    gcd = math.gcd(orig_sr, target_sr)
    up = target_sr // gcd
    down = orig_sr // gcd
    return scipy.signal.resample_poly(chunk.astype(np.float64), up, down)


def estimate_pitch(
    audio: np.ndarray, sr: int
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (f0, voiced_flag, voiced_probs) using librosa.pyin."""
    f0, voiced_flag, voiced_probs = librosa.pyin(
        audio.astype(np.float32),
        fmin=60,
        fmax=400,
        sr=sr,
        frame_length=1024,
        hop_length=256,
    )
    return f0, voiced_flag, voiced_probs


def frame_energy_rms(audio: np.ndarray) -> float:
    """Return RMS of the whole window in dBFS (relative to full scale)."""
    if audio.size == 0:
        return -np.inf
    rms = np.sqrt(np.mean(audio.astype(np.float64) ** 2))
    # dBFS assuming float range [-1, 1]
    return 20.0 * math.log10(max(rms, 1e-12))


def extract_unit(
    textgrid_path: Path,
    segment_wav: Path,
    token: str,
    espeak_phones: List[str],
    diphone_pair: List[str],
    arpa_phones: List[str],
    line_no: int,
    diphone_index: int,
    unit_type: str,
    align_analysis: Optional[Dict[str, Dict[str, float]]],
    args,
) -> Optional[Dict[str, object]]:
    """Extract one diphone unit and return its grid row + metadata."""
    flags: List[str] = []

    # Parse alignment
    try:
        tiers = parse_textgrid(textgrid_path)
    except Exception as exc:
        return {
            "line": line_no,
            "unit_type": unit_type,
            "token": token,
            "diphone": "_".join(diphone_pair),
            "phone1": diphone_pair[0],
            "phone2": diphone_pair[1],
            "flags": ["textgrid_parse_error"],
            "error": str(exc),
        }

    words = tiers.get("words", [])
    phones = tiers.get("phones", [])

    word_intervals = [w for w in words if w.get("text") == token]
    if not word_intervals:
        return {
            "line": line_no,
            "unit_type": unit_type,
            "token": token,
            "diphone": "_".join(diphone_pair),
            "phone1": diphone_pair[0],
            "phone2": diphone_pair[1],
            "flags": ["missing_target_word"],
        }
    word = word_intervals[0]
    word_start = float(word["xmin"])
    word_end = float(word["xmax"])

    # Phones inside the target word (non-empty, ordered)
    word_phones = [
        p
        for p in phones
        if p.get("text")
        and float(p["xmax"]) > word_start
        and float(p["xmin"]) < word_end
    ]
    word_phones.sort(key=lambda p: float(p["xmin"]))

    # Map eSpeak -> ARPAbet indices
    try:
        espeak_to_arpa = align_espeak_to_arpa(espeak_phones, arpa_phones)
    except Exception as exc:
        return {
            "line": line_no,
            "unit_type": unit_type,
            "token": token,
            "diphone": "_".join(diphone_pair),
            "phone1": diphone_pair[0],
            "phone2": diphone_pair[1],
            "flags": ["dictionary_mapping_error"],
            "error": str(exc),
        }

    expected_phone_count = len(arpa_phones)
    actual_phone_count = len(word_phones)
    if actual_phone_count != expected_phone_count:
        flags.append("phone_count_mismatch")
        # Try to continue anyway if counts are close
        if actual_phone_count < max(espeak_to_arpa.values(), key=max)[-1] + 1:
            return {
                "line": line_no,
                "unit_type": unit_type,
                "token": token,
                "diphone": "_".join(diphone_pair),
                "phone1": diphone_pair[0],
                "phone2": diphone_pair[1],
                "flags": flags,
                "expected_phones": expected_phone_count,
                "actual_phones": actual_phone_count,
            }

    # Locate the two phones of this diphone
    idx1 = espeak_to_arpa[diphone_pair[0]]
    idx2 = espeak_to_arpa[diphone_pair[1]]

    def phone_span(idxs: Tuple[int, ...]) -> Tuple[float, float]:
        starts = [float(word_phones[i]["xmin"]) for i in idxs]
        ends = [float(word_phones[i]["xmax"]) for i in idxs]
        return min(starts), max(ends)

    p1_start, p1_end = phone_span(idx1)
    p2_start, p2_end = phone_span(idx2)

    p1_dur = p1_end - p1_start
    p2_dur = p2_end - p2_start

    # Context-based diphone window centered on the phone boundary.
    # The goal is to capture the transition plus enough of each phone to be
    # recognizable, without pulling in neighbouring phones.
    pc = phoneme_class("data/phonemes.json")
    p1, p2 = diphone_pair[0], diphone_pair[1]

    # Phone 1 (left side of the boundary)
    if p1 in pc["diphthongs"] | pc["rhotic"]:
        # For VC with a diphthong/r-colored vowel, keep most of the vowel.
        pre = min(p1_dur * 0.75, 0.25)
    elif p1 in pc["stops"]:
        # CV/CVC: include the whole stop closure + release so the consonant
        # is not lost when concatenating.
        pre = min(p1_dur, 0.12)
    else:
        pre = max(p1_dur / 2.0, 0.04)

    # Phone 2 (right side of the boundary)
    if p2 in pc["diphthongs"] | pc["rhotic"]:
        # For CV with a diphthong/r-colored vowel, keep most of the vowel.
        post = min(p2_dur * 0.75, 0.25)
    elif p2 in pc["stops"] | pc["affricates"]:
        # For VC with a final stop/affricate, keep the full closure and a
        # little tail so the consonant is present (even if unreleased).
        post = min(p2_dur + 0.03, 0.15)
    elif p2 in pc["obstruents"]:
        post = max(p2_dur / 2.0, 0.05)
    else:
        post = max(p2_dur / 2.0, 0.04)

    window_start = max(word_start, p1_end - pre)
    window_end = min(word_end, p2_start + post)
    window_dur = window_end - window_start

    if window_dur <= 0:
        flags.append("non_positive_window")

    # Load audio
    try:
        audio, sr = sf.read(str(segment_wav), dtype="float64")
        if audio.ndim > 1:
            audio = np.mean(audio, axis=1)
    except Exception as exc:
        return {
            "line": line_no,
            "unit_type": unit_type,
            "token": token,
            "diphone": "_".join(diphone_pair),
            "phone1": diphone_pair[0],
            "phone2": diphone_pair[1],
            "flags": ["audio_read_error"],
            "error": str(exc),
        }

    if window_start >= 0 and window_end > window_start:
        chunk = extract_window(audio, sr, window_start, window_end, args.target_sr)
    else:
        chunk = np.array([], dtype=np.float64)

    # Diagnostics
    rms_db = frame_energy_rms(chunk)
    peak = float(np.max(np.abs(chunk))) if chunk.size else 0.0
    zcr = (
        float(np.mean(librosa.feature.zero_crossing_rate(chunk, hop_length=256)))
        if chunk.size
        else 0.0
    )

    # Pitch analysis
    if chunk.size >= 1024:
        f0, voiced_flag, voiced_probs = estimate_pitch(chunk, args.target_sr)
        voiced = voiced_flag.astype(bool)
        voiced_f0 = f0[voiced]
        voicing_fraction = float(np.mean(voiced)) if voiced.size else 0.0
        f0_mean = float(np.nanmean(voiced_f0)) if voiced_f0.size and np.any(np.isfinite(voiced_f0)) else None
        f0_std = float(np.nanstd(voiced_f0)) if voiced_f0.size and np.any(np.isfinite(voiced_f0)) else None
        f0_min = float(np.nanmin(voiced_f0)) if voiced_f0.size and np.any(np.isfinite(voiced_f0)) else None
        f0_max = float(np.nanmax(voiced_f0)) if voiced_f0.size and np.any(np.isfinite(voiced_f0)) else None
    else:
        f0 = voiced_flag = voiced_probs = np.array([])
        voicing_fraction = 0.0
        f0_mean = f0_std = f0_min = f0_max = None

    # Flags
    if p1_dur < args.min_phone_duration:
        flags.append("short_phone1")
    if p2_dur < args.min_phone_duration:
        flags.append("short_phone2")
    if window_dur < args.min_window_duration:
        flags.append("short_window")
    if word_start > args.long_leading_silence:
        flags.append("long_leading_silence")
    if rms_db < args.low_energy_db:
        flags.append("low_energy")
    if voicing_fraction < args.min_voicing:
        flags.append("mostly_unvoiced")

    # Alignment quality
    aa = align_analysis.get(textgrid_path.stem) if align_analysis else None
    overall_ll = aa.get("overall_log_likelihood") if aa else None
    snr = aa.get("snr") if aa else None

    unit_id = f"line_{line_no:04d}_{token}_{diphone_index}"

    row: Dict[str, object] = {
        "unit_id": unit_id,
        "line": line_no,
        "unit_type": unit_type,
        "token": token,
        "diphone": "_".join(diphone_pair),
        "phone1": diphone_pair[0],
        "phone2": diphone_pair[1],
        "source_wav": segment_wav.name,
        "textgrid": textgrid_path.name,
        "word_start_s": round(word_start, 6),
        "word_end_s": round(word_end, 6),
        "phone1_start_s": round(p1_start, 6),
        "phone1_end_s": round(p1_end, 6),
        "phone2_start_s": round(p2_start, 6),
        "phone2_end_s": round(p2_end, 6),
        "window_start_s": round(window_start, 6),
        "window_end_s": round(window_end, 6),
        "window_duration_s": round(window_dur, 6),
        "phone1_duration_s": round(p1_dur, 6),
        "phone2_duration_s": round(p2_dur, 6),
        "sample_rate": args.target_sr,
        "rms_db": round(rms_db, 3),
        "peak": round(peak, 6),
        "zcr": round(zcr, 6),
        "f0_mean_hz": round(f0_mean, 2) if f0_mean is not None else None,
        "f0_std_hz": round(f0_std, 2) if f0_std is not None else None,
        "f0_min_hz": round(f0_min, 2) if f0_min is not None else None,
        "f0_max_hz": round(f0_max, 2) if f0_max is not None else None,
        "voicing_fraction": round(voicing_fraction, 4),
        "overall_log_likelihood": round(overall_ll, 3) if overall_ll is not None else None,
        "snr_db": round(snr, 3) if snr is not None else None,
        "flags": ";".join(flags) if flags else "",
    }

    # Per-unit files
    meta = dict(row)
    meta["f0_contour_hz"] = [float(v) if np.isfinite(v) else None for v in f0]
    meta["voiced_flag"] = [bool(v) for v in voiced_flag]

    return {
        "row": row,
        "meta": meta,
        "chunk": chunk,
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def load_alignment_analysis(path: Path) -> Dict[str, Dict[str, float]]:
    """Load MFA alignment_analysis.csv keyed by file stem."""
    result: Dict[str, Dict[str, float]] = {}
    if not path.exists():
        return result
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            key = row["file"]
            result[key] = {
                "overall_log_likelihood": float(row["overall_log_likelihood"]),
                "snr": float(row["snr"]),
            }
    return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        default="output",
        help="Directory containing diphone_map.json, segments/, mfa_aligned/",
    )
    parser.add_argument(
        "--target-sr",
        type=int,
        default=22050,
        help="Sample rate for extracted unit WAVs (default: 22050)",
    )
    parser.add_argument(
        "--min-phone-duration",
        type=float,
        default=0.015,
        help="Flag a phone as too short if below this duration (seconds)",
    )
    parser.add_argument(
        "--min-window-duration",
        type=float,
        default=0.020,
        help="Flag a unit window as too short if below this duration (seconds)",
    )
    parser.add_argument(
        "--long-leading-silence",
        type=float,
        default=2.0,
        help="Flag target word start time beyond this many seconds",
    )
    parser.add_argument(
        "--low-energy-db",
        type=float,
        default=-60.0,
        help="Flag unit RMS below this dBFS",
    )
    parser.add_argument(
        "--min-voicing",
        type=float,
        default=0.1,
        help="Flag a unit as mostly unvoiced if voicing fraction is below this",
    )
    args = parser.parse_args(argv)

    output_dir = Path(args.output_dir)
    if not output_dir.is_dir():
        print(f"Output directory not found: {output_dir}", file=sys.stderr)
        return 1

    segments_dir = output_dir / "segments"
    aligned_dir = output_dir / "mfa_aligned"

    diphone_map_path = output_dir / "diphone_map.json"
    espeak_dict_path = output_dir / "dictionary.txt"
    arpa_dict_path = output_dir / "mfa_dictionary.txt"
    analysis_path = aligned_dir / "alignment_analysis.csv"

    for p in (diphone_map_path, espeak_dict_path, arpa_dict_path, segments_dir, aligned_dir):
        if not p.exists():
            print(f"Required input missing: {p}", file=sys.stderr)
            return 1

    print("Loading dictionaries and diphone map...")
    diphone_map = json.loads(diphone_map_path.read_text(encoding="utf-8"))
    espeak_dict = load_dictionary(espeak_dict_path)
    arpa_dict = load_dictionary(arpa_dict_path)
    align_analysis = load_alignment_analysis(analysis_path)

    units_dir = output_dir / "units"
    grid_dir = output_dir / "unit_grid"
    units_dir.mkdir(parents=True, exist_ok=True)
    grid_dir.mkdir(parents=True, exist_ok=True)

    grid_rows: List[Dict[str, object]] = []
    failed: List[Dict[str, object]] = []
    total_units = 0

    lines = diphone_map.get("lines", [])
    print(f"Processing {len(lines)} script lines...")

    for entry in lines:
        line_no = int(entry["line"])
        token = entry["token"]
        unit_type = entry["unit_type"]
        espeak_phones = entry["phones"]
        diphones = entry["diphones"]

        textgrid_path = aligned_dir / f"line_{line_no:04d}_{token}.TextGrid"
        segment_wav = segments_dir / f"line_{line_no:04d}_{token}.wav"

        if token not in espeak_dict or token not in arpa_dict:
            for i, dp in enumerate(diphones):
                failed.append({
                    "line": line_no,
                    "unit_type": unit_type,
                    "token": token,
                    "diphone": "_".join(dp),
                    "flags": ["missing_dictionary_entry"],
                })
            continue

        arpa_phones = arpa_dict[token]

        for i, dp in enumerate(diphones):
            total_units += 1
            result = extract_unit(
                textgrid_path,
                segment_wav,
                token,
                espeak_phones,
                dp,
                arpa_phones,
                line_no,
                i,
                unit_type,
                align_analysis,
                args,
            )
            if result is None:
                continue

            if "row" not in result:
                failed.append(result)
                continue

            row = result["row"]
            chunk = result["chunk"]
            meta = result["meta"]

            grid_rows.append(row)

            unit_id = str(row["unit_id"])
            subdir = units_dir / safe_filename(unit_type)
            subdir.mkdir(parents=True, exist_ok=True)
            wav_path = subdir / f"{safe_filename(unit_id)}.wav"
            json_path = subdir / f"{safe_filename(unit_id)}.json"

            if chunk.size:
                sf.write(str(wav_path), chunk, args.target_sr)
            json_path.write_text(
                json.dumps(meta, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )

        if line_no % 100 == 0:
            print(f"  ...processed up to line {line_no} ({len(grid_rows)} units extracted)")

    # Write grid files
    grid_csv = grid_dir / "unit_grid.csv"
    grid_json = grid_dir / "unit_grid.json"

    if grid_rows:
        fieldnames = list(grid_rows[0].keys())
        with grid_csv.open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(grid_rows)

        grid_json.write_text(
            json.dumps({"units": grid_rows, "metadata": vars(args)}, indent=2),
            encoding="utf-8",
        )

    # Summary report
    n_grid = len(grid_rows)
    n_failed = len(failed)
    flag_counts: Dict[str, int] = {}
    for row in grid_rows:
        for flg in str(row.get("flags", "")).split(";"):
            if flg:
                flag_counts[flg] = flag_counts.get(flg, 0) + 1
    for frow in failed:
        for flg in frow.get("flags", []):
            flag_counts[flg] = flag_counts.get(flg, 0) + 1

    report_lines = [
        "Diphone Unit Grid Report",
        "========================",
        f"Script lines processed: {len(lines)}",
        f"Diphone units expected: {total_units}",
        f"Units extracted to grid: {n_grid}",
        f"Units/failed entries: {n_failed}",
        "",
        "Flag counts:",
    ]
    for flg, cnt in sorted(flag_counts.items(), key=lambda kv: -kv[1]):
        report_lines.append(f"  {flg}: {cnt}")

    report_path = grid_dir / "unit_grid_report.txt"
    report_path.write_text("\n".join(report_lines) + "\n", encoding="utf-8")

    print("\n".join(report_lines))
    print(f"\nGrid written to: {grid_csv}")
    print(f"Report written to: {report_path}")
    print(f"Unit files written to: {units_dir}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
