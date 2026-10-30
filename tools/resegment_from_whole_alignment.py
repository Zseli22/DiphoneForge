#!/usr/bin/env python3
"""
Re-segment the raw recordings using whole-file forced alignment.

For each raw WAV we know exactly which script lines it contains (from
voice_file_ranges.txt). We build a transcript of the full sequence of
"say <token>" prompts, align the entire file with MFA, and then cut the
file into per-line WAVs using the aligned word boundaries.

After cutting, each per-line segment is aligned again with MFA for
phone-level boundaries.

Usage:
    python tools/resegment_from_whole_alignment.py
    python tools/resegment_from_whole_alignment.py --audio-dir Voice_audio --output-dir output
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import soundfile as sf


# ---------------------------------------------------------------------------
# TextGrid parsing (short "ooTextFile" format only)
# ---------------------------------------------------------------------------


def parse_textgrid(path: Path) -> Dict[str, List[Dict[str, object]]]:
    """Return a dict mapping tier name -> list of intervals."""
    text = path.read_text(encoding="utf-8")
    lines = [ln.strip() for ln in text.splitlines()]

    # Remove header lines up to and including the first "size" line
    while lines and not lines[0].startswith("size"):
        lines.pop(0)
    if lines:
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

        m = re.match(r'name\s*=\s*"([^"]*)"', line)
        if m:
            current_tier = m.group(1)
            pending = {}
            i += 1
            continue

        m = re.match(r"intervals\s*\[(\d+)\]:", line)
        if m:
            if pending and "text" in pending:
                intervals.append(pending)
            pending = {}
            i += 1
            continue

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
# Helpers
# ---------------------------------------------------------------------------


def load_ranges(path: Path) -> Dict[str, Tuple[int, int]]:
    ranges: Dict[str, Tuple[int, int]] = {}
    with path.open(encoding="utf-8") as f:
        for raw in f:
            ln = raw.strip()
            if not ln or ln.startswith("#"):
                continue
            parts = ln.split()
            if len(parts) >= 3:
                ranges[parts[0]] = (int(parts[1]), int(parts[2]))
    return ranges


def load_diphone_map(path: Path) -> Dict[int, Dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {entry["line"]: entry for entry in data["lines"]}


def clean_dictionary(in_path: Path, out_path: Path) -> None:
    """Write a dictionary without duplicate entries."""
    seen: set[str] = set()
    with in_path.open(encoding="utf-8") as fin, out_path.open("w", encoding="utf-8") as fout:
        for ln in fin:
            stripped = ln.strip()
            if not stripped or stripped.startswith("#"):
                continue
            word = stripped.split(None, 1)[0]
            if word in seen:
                continue
            seen.add(word)
            fout.write(ln)


# ---------------------------------------------------------------------------
# MFA invocation
# ---------------------------------------------------------------------------


def mfa_env() -> Dict[str, str]:
    """Return an environment dict with MFA binaries on PATH."""
    env = os.environ.copy()
    mfa_root = r"C:\diphone-mfa-env"
    prepend = os.pathsep.join([mfa_root, os.path.join(mfa_root, "Scripts"), os.path.join(mfa_root, "Library", "bin")])
    env["PATH"] = prepend + os.pathsep + env.get("PATH", "")
    return env


def run_mfa_align(
    corpus_dir: Path,
    dictionary_path: Path,
    output_dir: Path,
    beam: int = 10,
    retry_beam: int = 40,
    verbose: bool = False,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        r"C:\diphone-mfa-env\python.exe",
        "-m",
        "montreal_forced_aligner.command_line.mfa",
        "align",
        str(corpus_dir),
        str(dictionary_path),
        "english_us_arpa",
        str(output_dir),
        "--clean",
        "--beam",
        str(beam),
        "--retry_beam",
        str(retry_beam),
    ]
    if verbose:
        cmd.append("--verbose")
    print("Running MFA:\n  " + " ".join(cmd))
    subprocess.run(cmd, env=mfa_env(), check=True)


# ---------------------------------------------------------------------------
# Whole-file segmentation
# ---------------------------------------------------------------------------


def build_whole_file_corpus(
    audio_dir: Path,
    ranges: Dict[str, Tuple[int, int]],
    diphone_map: Dict[int, Dict],
    corpus_dir: Path,
    carrier: bool = True,
) -> List[str]:
    """Copy WAVs and write whole-file .lab transcripts. Returns processed basenames."""
    corpus_dir.mkdir(parents=True, exist_ok=True)
    basenames: List[str] = []

    wav_paths = sorted(audio_dir.glob("*.WAV"), key=lambda p: p.name)
    for wav_path in wav_paths:
        if wav_path.name not in ranges:
            print(f"WARNING: no range for {wav_path.name}, skipping")
            continue
        first, last = ranges[wav_path.name]
        tokens: List[str] = []
        missing: List[int] = []
        for line_no in range(first, last + 1):
            entry = diphone_map.get(line_no)
            if entry is None:
                missing.append(line_no)
                continue
            tokens.append(entry["token"])
        if missing:
            print(f"WARNING: {wav_path.name} missing diphone_map entries for lines {missing}")
            continue

        transcript = " ".join(tokens) if not carrier else " ".join(f"say {t}" for t in tokens)
        base = wav_path.stem
        lab_path = corpus_dir / f"{base}.lab"
        lab_path.write_text(transcript, encoding="utf-8")
        shutil.copy2(wav_path, corpus_dir / wav_path.name)
        basenames.append(base)

    return basenames


def cut_segments_from_textgrid(
    textgrid_path: Path,
    wav_path: Path,
    diphone_map: Dict[int, Dict],
    first_line: int,
    last_line: int,
    segments_dir: Path,
    pad_start_s: float = 0.05,
    pad_end_s: float = 0.1,
    carrier: bool = True,
) -> List[Dict[str, object]]:
    """Cut per-line WAVs from a whole-file alignment TextGrid."""
    tiers = parse_textgrid(textgrid_path)
    words = tiers.get("words", [])

    # Build ordered list of (line_no, token, say_start, token_end)
    token_to_line = {entry["token"]: line_no for line_no, entry in diphone_map.items()}
    utterances: List[Dict[str, object]] = []
    prev_say_start: Optional[float] = None
    prev_say_end: Optional[float] = None

    for i, w in enumerate(words):
        text = str(w.get("text", "")).strip()
        if text == "say":
            prev_say_start = float(w["xmin"])
            prev_say_end = float(w["xmax"])
            continue
        if text and text in token_to_line:
            line_no = token_to_line[text]
            token_start = float(w["xmin"])
            token_end = float(w["xmax"])
            utterances.append({
                "line": line_no,
                "token": text,
                "say_start": prev_say_start if prev_say_start is not None else token_start,
                "say_end": prev_say_end if prev_say_end is not None else token_start,
                "token_start": token_start,
                "token_end": token_end,
            })
            prev_say_start = None
            prev_say_end = None

    # Sort by line number and check completeness
    utterances.sort(key=lambda u: u["line"])
    expected_lines = set(range(first_line, last_line + 1))
    found_lines = {u["line"] for u in utterances}
    missing = expected_lines - found_lines
    if missing:
        print(f"WARNING: {wav_path.name} missing aligned tokens for lines {sorted(missing)}")

    audio, sr = sf.read(str(wav_path), dtype="float64")
    if audio.ndim > 1:
        audio = audio.mean(axis=1)

    rows: List[Dict[str, object]] = []
    for idx, u in enumerate(utterances):
        line_no = u["line"]
        entry = diphone_map[line_no]

        # Segment boundaries: with the carrier, from this "say" to the next one.
        # Without it, pad the token itself so the clip has context on both sides.
        if carrier:
            start_s = u["say_start"] - pad_start_s
            if idx + 1 < len(utterances):
                end_s = utterances[idx + 1]["say_start"] + pad_end_s
            else:
                end_s = u["token_end"] + pad_end_s
        else:
            start_s = u["token_start"] - pad_start_s
            end_s = u["token_end"] + pad_end_s
            if idx + 1 < len(utterances):
                end_s = min(end_s, utterances[idx + 1]["token_start"] + 0.02)
            end_s = max(end_s, u["token_end"])

        start_s = max(0.0, start_s)
        end_s = min(len(audio) / sr, end_s)

        start_sample = int(round(start_s * sr))
        end_sample = int(round(end_s * sr))
        segment = audio[start_sample:end_sample]

        token = entry["token"]
        segment_file = f"line_{line_no:04d}_{token}.wav"
        out_path = segments_dir / segment_file
        sf.write(str(out_path), segment, sr, subtype="PCM_16")

        # Write per-line .lab for later phone-level alignment
        lab_path = out_path.with_suffix(".lab")
        lab_path.write_text(f"say {token}" if carrier else token, encoding="utf-8")

        rows.append({
            "segment_file": segment_file,
            "source_file": wav_path.name,
            "source_start_s": round(start_s, 6),
            "source_end_s": round(end_s, 6),
            "duration_s": round(end_s - start_s, 6),
            "script_line": line_no,
            "script_text": f"Say {' '.join(entry['phones'])}",
            "token": token,
            "diphones": json.dumps(entry["diphones"]),
        })

    return rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio-dir", default="Voice_audio", help="Directory with raw .WAV recordings")
    parser.add_argument("--output-dir", default="output", help="Project output directory")
    parser.add_argument("--ranges", default="voice_file_ranges.txt", help="File-to-line-range mapping")
    parser.add_argument("--diphone-map", default="output/diphone_map.json", help="Path to diphone_map.json")
    parser.add_argument("--dictionary", default="output/mfa_dictionary.txt", help="Path to MFA dictionary")
    parser.add_argument("--whole-beam", type=int, default=100, help="Beam for whole-file alignment")
    parser.add_argument("--whole-retry-beam", type=int, default=400, help="Retry beam for whole-file alignment")
    parser.add_argument("--line-beam", type=int, default=40, help="Beam for per-line alignment")
    parser.add_argument("--line-retry-beam", type=int, default=160, help="Retry beam for per-line alignment")
    parser.add_argument("--skip-whole-align", action="store_true", help="Skip whole-file MFA alignment (use existing output/_whole_aligned)")
    parser.add_argument("--skip-line-align", action="store_true", help="Skip per-line MFA alignment")
    parser.add_argument(
        "--no-carrier",
        action="store_true",
        help="Transcripts contain only the target token, not an unspoken 'say' carrier.",
    )
    parser.add_argument("--verbose", action="store_true", help="Verbose MFA output")
    args = parser.parse_args(argv)

    audio_dir = Path(args.audio_dir)
    output_dir = Path(args.output_dir)
    ranges_path = Path(args.ranges)
    diphone_map_path = Path(args.diphone_map)
    dictionary_path = Path(args.dictionary)

    for p in (audio_dir, ranges_path, diphone_map_path, dictionary_path):
        if not p.exists():
            print(f"Required input not found: {p}", file=sys.stderr)
            return 1

    ranges = load_ranges(ranges_path)
    diphone_map = load_diphone_map(diphone_map_path)
    carrier = not args.no_carrier

    whole_corpus_dir = output_dir / "_whole_corpus"
    whole_aligned_dir = output_dir / "_whole_aligned"
    segments_dir = output_dir / "segments"
    line_aligned_dir = output_dir / "mfa_aligned"

    segments_dir.mkdir(parents=True, exist_ok=True)

    # Clean dictionary for MFA (remove duplicate say entries)
    clean_dict_path = output_dir / "mfa_dictionary_clean.txt"
    clean_dictionary(dictionary_path, clean_dict_path)

    # ------------------------------------------------------------------
    # Step 1: whole-file alignment
    # ------------------------------------------------------------------
    if not args.skip_whole_align:
        print("\n=== Step 1: preparing whole-file corpus ===")
        if whole_corpus_dir.exists():
            shutil.rmtree(whole_corpus_dir)
        if whole_aligned_dir.exists():
            shutil.rmtree(whole_aligned_dir)
        basenames = build_whole_file_corpus(audio_dir, ranges, diphone_map, whole_corpus_dir, carrier=carrier)
        print(f"Prepared {len(basenames)} whole-file transcripts in {whole_corpus_dir}")

        print("\n=== Step 2: whole-file forced alignment ===")
        run_mfa_align(
            whole_corpus_dir,
            clean_dict_path,
            whole_aligned_dir,
            beam=args.whole_beam,
            retry_beam=args.whole_retry_beam,
            verbose=args.verbose,
        )
    else:
        print("Skipping whole-file alignment (using existing output/_whole_aligned)")

    # ------------------------------------------------------------------
    # Step 3: cut per-line segments from whole-file alignments
    # ------------------------------------------------------------------
    print("\n=== Step 3: cutting per-line segments ===")
    all_rows: List[Dict[str, object]] = []
    for wav_path in sorted(audio_dir.glob("*.WAV"), key=lambda p: p.name):
        if wav_path.name not in ranges:
            continue
        base = wav_path.stem
        textgrid_path = whole_aligned_dir / f"{base}.TextGrid"
        if not textgrid_path.exists():
            print(f"WARNING: no whole-file TextGrid for {wav_path.name}")
            continue
        first, last = ranges[wav_path.name]
        rows = cut_segments_from_textgrid(
            textgrid_path,
            whole_corpus_dir / wav_path.name,
            diphone_map,
            first,
            last,
            segments_dir,
            carrier=carrier,
        )
        all_rows.extend(rows)
        print(f"  {wav_path.name}: cut {len(rows)} segments")

    # Write segments manifest
    manifest_path = segments_dir / "segments.csv"
    fieldnames = ["segment_file", "source_file", "source_start_s", "source_end_s", "duration_s", "script_line", "script_text", "token", "diphones"]
    with manifest_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_rows)
    print(f"Wrote {manifest_path} ({len(all_rows)} segments)")

    # ------------------------------------------------------------------
    # Step 4: per-line phone-level alignment
    # ------------------------------------------------------------------
    if not args.skip_line_align:
        print("\n=== Step 4: per-line phone-level alignment ===")
        if line_aligned_dir.exists():
            shutil.rmtree(line_aligned_dir)
        run_mfa_align(
            segments_dir,
            clean_dict_path,
            line_aligned_dir,
            beam=args.line_beam,
            retry_beam=args.line_retry_beam,
            verbose=args.verbose,
        )
        print(f"Per-line alignments written to {line_aligned_dir}")

    print("\nDone. Review segments.csv and the per-line TextGrids before building units.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
