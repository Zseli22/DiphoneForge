#!/usr/bin/env python3
"""
Analyze the unit grid produced by ``build_unit_grid.py`` and generate a
human-readable diagnostic report.

Usage:
    python tools/analyze_unit_grid.py
    python tools/analyze_unit_grid.py --output-dir output
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Optional


def load_phonemes(path: Path) -> Dict[str, List[str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        "vowels": data.get("vowels", []),
        "consonants": data.get("consonants", []),
    }


def mean_or_none(values: List[float]) -> Optional[float]:
    return statistics.mean(values) if values else None


def median_or_none(values: List[float]) -> Optional[float]:
    return statistics.median(values) if values else None


def stdev_or_none(values: List[float]) -> Optional[float]:
    return statistics.stdev(values) if len(values) > 1 else (0.0 if values else None)


def percentiles(values: List[float], ps=(0.05, 0.25, 0.5, 0.75, 0.95)) -> List[float]:
    if not values:
        return []
    s = sorted(values)
    return [s[min(len(s) - 1, int(p * len(s)))] for p in ps]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        default="output",
        help="Directory containing unit_grid/unit_grid.csv",
    )
    parser.add_argument(
        "--voicing-threshold",
        type=float,
        default=0.25,
        help="Fraction below which a unit is considered unvoiced",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    grid_csv = output_dir / "unit_grid" / "unit_grid.csv"
    phonemes_path = Path("data") / "phonemes.json"
    if not grid_csv.exists():
        print(f"Unit grid not found: {grid_csv}", file=__import__("sys").stderr)
        return 1

    phonemes = load_phonemes(phonemes_path)
    vowels = set(phonemes["vowels"])

    rows = list(csv.DictReader(grid_csv.open(encoding="utf-8")))

    # Per-unit-type buckets
    by_type: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for r in rows:
        by_type[r["unit_type"]].append(r)

    def parse_float(r: Dict[str, str], key: str) -> Optional[float]:
        v = r.get(key, "")
        return float(v) if v not in ("", "None") else None

    # Overall numeric stats
    durations = [parse_float(r, "window_duration_s") for r in rows]
    durations = [v for v in durations if v is not None]
    f0s = [parse_float(r, "f0_mean_hz") for r in rows]
    f0s = [v for v in f0s if v is not None]
    snrs = [parse_float(r, "snr_db") for r in rows]
    snrs = [v for v in snrs if v is not None]

    all_flags = Counter()
    for r in rows:
        for flg in r.get("flags", "").split(";"):
            if flg:
                all_flags[flg] += 1

    # Per-type stats
    type_report: List[str] = []
    for utype in sorted(by_type):
        bucket = by_type[utype]
        ds = [parse_float(r, "window_duration_s") for r in bucket]
        ds = [v for v in ds if v is not None]
        fs = [parse_float(r, "f0_mean_hz") for r in bucket]
        fs = [v for v in fs if v is not None]
        ss = [parse_float(r, "snr_db") for r in bucket]
        ss = [v for v in ss if v is not None]
        type_report.append(
            f"  {utype:6s}: n={len(bucket):4d}, dur={mean_or_none(ds):.3f}s "
            f"(median {median_or_none(ds):.3f}, min {min(ds) if ds else None:.3f}, "
            f"max {max(ds) if ds else None:.3f}), f0_mean={mean_or_none(fs):.1f} Hz, "
            f"SNR={mean_or_none(ss):.1f} dB"
        )

    # Quality issue detection
    problem_rows: List[Dict[str, str]] = []
    for r in rows:
        flags = r.get("flags", "").split(";") if r.get("flags") else []
        has_structural_issue = any(
            f in flags
            for f in ("short_window", "low_energy", "phone_count_mismatch", "missing_target_word")
        )
        has_short_phone = "short_phone1" in flags or "short_phone2" in flags

        # A unit is expected to have voicing if either phone is a vowel/sonorant.
        # Here we use vowels as the simple proxy.
        expected_voiced = r["phone1"] in vowels or r["phone2"] in vowels
        vf = parse_float(r, "voicing_fraction") or 0.0
        voicing_issue = expected_voiced and vf < args.voicing_threshold

        if has_structural_issue or voicing_issue or has_short_phone:
            problem_rows.append(r)

    # Per-diphone mean duration and voicing
    by_diphone: Dict[str, List[Dict[str, str]]] = defaultdict(list)
    for r in rows:
        by_diphone[r["diphone"]].append(r)

    diphone_variability: List[Tuple[str, int, float, float, float]] = []
    for diphone, bucket in by_diphone.items():
        ds = [parse_float(r, "window_duration_s") for r in bucket if parse_float(r, "window_duration_s") is not None]
        vfs = [parse_float(r, "voicing_fraction") for r in bucket if parse_float(r, "voicing_fraction") is not None]
        if len(bucket) > 1 and ds:
            diphone_variability.append(
                (
                    diphone,
                    len(bucket),
                    statistics.mean(ds),
                    statistics.stdev(ds) if len(ds) > 1 else 0.0,
                    statistics.mean(vfs) if vfs else 0.0,
                )
            )
    diphone_variability.sort(key=lambda t: -t[3])  # highest duration std first

    report_lines = [
        "Diphone Unit Grid Diagnostics",
        "============================",
        f"Total units in grid: {len(rows)}",
        f"Unique diphone labels: {len(by_diphone)}",
        "",
        "Overall distributions",
        f"  Window duration: mean={mean_or_none(durations):.3f}s, median={median_or_none(durations):.3f}s",
        f"    percentiles (5/25/50/75/95): "
        + ", ".join(f"{v:.3f}s" for v in percentiles(durations)),
        f"  F0 (voiced units): mean={mean_or_none(f0s):.1f} Hz, median={median_or_none(f0s):.1f} Hz",
        f"    percentiles (5/25/50/75/95): "
        + ", ".join(f"{v:.1f} Hz" for v in percentiles(f0s)),
        f"  SNR: mean={mean_or_none(snrs):.1f} dB, median={median_or_none(snrs):.1f} dB",
        "",
        "Units by type",
    ]
    report_lines.extend(type_report)

    report_lines.extend([
        "",
        "Flag counts (all units)",
    ])
    for flg, cnt in all_flags.most_common():
        report_lines.append(f"  {flg}: {cnt}")

    report_lines.extend([
        "",
        f"Quality-flagged units: {len(problem_rows)} ({100*len(problem_rows)/len(rows):.1f}%)",
        "(structural issues, very short phones, or expected-voiced units with low voicing)",
    ])

    # Diphone duration variability
    report_lines.extend([
        "",
        "Diphone labels with highest duration variability (only labels recorded more than once)",
    ])
    for diphone, n, mean_dur, std_dur, mean_vf in diphone_variability[:20]:
        report_lines.append(
            f"  {diphone}: n={n}, dur={mean_dur:.3f}s ± {std_dur:.3f}s, vf={mean_vf:.2f}"
        )

    report_lines.extend([
        "",
        "Worst-quality units (sample)",
    ])
    # Sort by low SNR then by short duration
    sorted_problems = sorted(
        problem_rows,
        key=lambda r: (
            parse_float(r, "snr_db") or -999.0,
            parse_float(r, "window_duration_s") or 999.0,
        ),
    )
    for r in sorted_problems[:30]:
        report_lines.append(
            f"  {r['unit_id']} ({r['diphone']}): dur={parse_float(r,'window_duration_s'):.3f}s, "
            f"vf={parse_float(r,'voicing_fraction'):.2f}, snr={parse_float(r,'snr_db'):.1f}dB, "
            f"flags={r.get('flags','(none)')}"
        )

    report_text = "\n".join(report_lines) + "\n"
    report_path = output_dir / "unit_grid" / "diagnostics_report.txt"
    report_path.write_text(report_text, encoding="utf-8")
    print(report_text)
    print(f"Report saved to: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
