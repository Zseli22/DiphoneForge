#!/usr/bin/env python3
"""
Validate segmented utterances and flag outliers for manual review.

Usage:
    python tools/validate_segments.py --segments-dir output/segments
"""

import argparse
import csv
import json
import statistics
from pathlib import Path

import numpy as np
import soundfile as sf


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate segmented utterances.")
    parser.add_argument(
        "--segments-dir",
        type=Path,
        default=Path("output/segments"),
        help="Directory containing segmented WAV files.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("output/segments/segments.csv"),
        help="Path to segments.csv manifest.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("output/segments/validation_report.json"),
        help="Where to write the validation report.",
    )
    parser.add_argument(
        "--short-ms",
        type=float,
        default=500,
        help="Flag segments shorter than this many milliseconds.",
    )
    parser.add_argument(
        "--long-ms",
        type=float,
        default=3500,
        help="Flag segments longer than this many milliseconds.",
    )
    parser.add_argument(
        "--quiet-sigma",
        type=float,
        default=2.5,
        help="Flag segments whose RMS is this many standard deviations below the mean.",
    )
    parser.add_argument(
        "--loud-sigma",
        type=float,
        default=2.5,
        help="Flag segments whose RMS is this many standard deviations above the mean.",
    )
    return parser.parse_args()


def rms_db(audio: np.ndarray) -> float:
    rms = np.sqrt(np.mean(audio ** 2))
    return 20 * np.log10(max(rms, 1e-10))


def main() -> None:
    args = parse_args()

    if not args.manifest.exists():
        raise FileNotFoundError(args.manifest)

    records = []
    with args.manifest.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            records.append(row)

    # First pass: load every segment and collect duration/RMS.
    measurements = []
    flagged = []

    for row in records:
        seg_path = args.segments_dir / row["segment_file"]
        if not seg_path.exists():
            flagged.append({
                "segment_file": row["segment_file"],
                "reason": "file_missing",
            })
            continue

        audio, sr = sf.read(str(seg_path), dtype="float32")
        if audio.ndim > 1:
            audio = np.mean(audio, axis=1)

        dur_ms = len(audio) / sr * 1000
        rms = rms_db(audio)
        measurements.append({
            "row": row,
            "dur_ms": dur_ms,
            "rms_db": rms,
        })

    durations = [m["dur_ms"] for m in measurements]
    rms_values = [m["rms_db"] for m in measurements]
    mean_dur = statistics.mean(durations)
    stdev_dur = statistics.stdev(durations) if len(durations) > 1 else 0
    mean_rms = statistics.mean(rms_values)
    stdev_rms = statistics.stdev(rms_values) if len(rms_values) > 1 else 0

    # Second pass: flag outliers using relative thresholds.
    for m in measurements:
        row = m["row"]
        dur_ms = m["dur_ms"]
        rms = m["rms_db"]

        reasons = []
        if dur_ms < args.short_ms:
            reasons.append("too_short")
        if dur_ms > args.long_ms:
            reasons.append("too_long")
        if rms < mean_rms - args.quiet_sigma * stdev_rms:
            reasons.append("too_quiet")
        if rms > mean_rms + args.loud_sigma * stdev_rms:
            reasons.append("too_loud")

        if reasons:
            flagged.append({
                "segment_file": row["segment_file"],
                "script_line": int(row["script_line"]),
                "script_text": row["script_text"],
                "duration_ms": round(float(dur_ms), 1),
                "rms_db": round(float(rms), 1),
                "reasons": reasons,
            })

    print(f"Validated {len(records)} segments.")
    print(f"Duration: mean={mean_dur:.0f} ms, stdev={stdev_dur:.0f} ms")
    print(f"RMS: mean={mean_rms:.1f} dBFS, stdev={stdev_rms:.1f} dB")
    print(f"Flagged segments: {len(flagged)}")

    # Show a sample of flagged items
    if flagged:
        print("\nSample flagged segments:")
        for item in flagged[:20]:
            print(f"  {item['segment_file']} line {item.get('script_line', '?')}: "
                  f"{item.get('duration_ms', '?')} ms, {item.get('rms_db', '?')} dBFS, "
                  f"reasons={item['reasons']}")
        if len(flagged) > 20:
            print(f"  ... and {len(flagged) - 20} more.")

    report = {
        "segment_count": len(records),
        "duration_ms": {
            "mean": round(float(mean_dur), 2),
            "stdev": round(float(stdev_dur), 2),
            "min": round(float(min(durations)), 2),
            "max": round(float(max(durations)), 2),
        },
        "rms_dbfs": {
            "mean": round(float(mean_rms), 2),
            "stdev": round(float(stdev_rms), 2),
            "min": round(float(min(rms_values)), 2),
            "max": round(float(max(rms_values)), 2),
        },
        "flagged_count": len(flagged),
        "flagged": flagged,
    }

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
    print(f"\nWrote validation report: {args.output}")


if __name__ == "__main__":
    main()
