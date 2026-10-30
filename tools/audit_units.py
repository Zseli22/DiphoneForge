#!/usr/bin/env python3
"""Audit every aligned unit against the original recordings.

For each script line this checks, using the original source WAV rather than
the derived clip:

  * whether the aligned word span contains usable speech energy
  * whether the segment window is silent or only captures a tail
  * whether usable speech exists just outside the segment (repairable offset)
  * clipping, edge-truncation, and level outliers

Outputs (under output/unit_quality/):
  line_audit.csv     one row per script line
  unit_audit.csv     one row per unit, inheriting the line verdict
  report.txt         summary and grouped worst cases
  rerecord_lines.txt  lines with no recoverable speech
  repair_lines.txt    lines whose speech is present but misaligned/cut
  quiet_lines.txt     lines that are present but unusually quiet

Usage:
    python tools/audit_units.py
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parent.parent
VOICE_AUDIO = ROOT / "Voice_audio"
SEGMENTS = ROOT / "output" / "segments" / "segments.csv"
UNIT_GRID = ROOT / "output" / "unit_grid" / "unit_grid.csv"
RANGES = ROOT / "voice_file_ranges.txt"
OUT_DIR = ROOT / "output" / "unit_quality"

FRAME = 0.02
HOP = 0.01
SILENT_PEAK = 0.010      # below this the span is effectively room tone
QUIET_PEAK = 0.030       # below this it is audible but much weaker than corpus
CLIP_LEVEL = 0.999
EDGE_MARGIN = 0.025      # speech this close to a segment edge looks truncated
NEARBY = 1.5             # seconds to search for displaced speech


def load_lines() -> dict[int, dict]:
    with SEGMENTS.open(encoding="utf-8", newline="") as handle:
        return {int(r["script_line"]): r for r in csv.DictReader(handle)}


def load_units() -> list[dict]:
    with UNIT_GRID.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def load_ranges() -> list[tuple[str, int, int]]:
    result = []
    for raw in RANGES.read_text(encoding="utf-8").splitlines():
        raw = raw.strip()
        if not raw or raw.startswith("#"):
            continue
        m = re.match(r"(\S+)\s+(\d+)\s+(\d+)", raw)
        if m:
            result.append((m.group(1), int(m.group(2)), int(m.group(3))))
    return result


def frame_db(x: np.ndarray, sr: int) -> np.ndarray:
    fl, hop = int(FRAME * sr), int(HOP * sr)
    if len(x) < fl:
        return np.array([])
    count = 1 + (len(x) - fl) // hop
    idx = np.arange(count)[:, None] * hop + np.arange(fl)[None, :]
    rms = np.sqrt((x[idx].astype(np.float64) ** 2).mean(axis=1))
    return 20.0 * np.log10(np.maximum(rms, 1e-10))


def find_events(x: np.ndarray, sr: int, floor_db: float, peak_db: float) -> list[tuple[float, float]]:
    db = frame_db(x, sr)
    if not len(db):
        return []
    threshold = max(floor_db + 10.0, floor_db + 0.30 * (peak_db - floor_db))
    above = db > threshold
    idx = np.flatnonzero(above)
    if not len(idx):
        return []
    fl, hop = int(FRAME * sr), int(HOP * sr)
    runs = []
    start = prev = idx[0]
    for i in idx[1:]:
        if i - prev <= 12:  # merge gaps up to 0.12 s
            prev = i
        else:
            runs.append((start, prev))
            start = prev = i
    runs.append((start, prev))
    events = []
    for a, b in runs:
        t0, t1 = a * hop / sr, (b * hop + fl) / sr
        if t1 - t0 >= 0.05:
            events.append((t0, t1))
    return events


def span_db(events: list[tuple[float, float]], start: float, end: float) -> float:
    """Longest overlap between an event and the span."""
    best = 0.0
    for a, b in events:
        best = max(best, min(b, end) - max(a, start))
    return max(best, 0.0)


def audit_file(fname: str, lines: list[dict], word_spans: dict[int, tuple[float, float]]) -> list[dict]:
    path = VOICE_AUDIO / fname
    x, sr = sf.read(path, dtype="float32", always_2d=True)
    if x.shape[1] > 1:
        x = x.mean(axis=1)
    db = frame_db(x, sr)
    floor = float(np.percentile(db, 10)) if len(db) else -90.0
    peak = float(db.max()) if len(db) else -90.0
    events = find_events(x, sr, floor, peak)
    rows = []
    for line in lines:
        s, e = float(line["source_start_s"]), float(line["source_end_s"])
        seg = x[int(s * sr):int(e * sr)]
        seg_peak = float(np.abs(seg).max()) if len(seg) else 0.0
        seg_db = frame_db(seg, sr)
        seg_floor = float(np.percentile(seg_db, 10)) if len(seg_db) else -90.0
        seg_speech = float(np.mean(seg_db > seg_floor + 10.0)) if len(seg_db) else 0.0

        ws = s + word_spans[int(line["script_line"])][0]
        we = s + word_spans[int(line["script_line"])][1]
        word = x[int(ws * sr):int(we * sr)]
        word_peak = float(np.abs(word).max()) if len(word) else 0.0
        word_db = frame_db(word, sr)
        word_rms = float(np.sqrt(np.mean(word.astype(np.float64) ** 2))) if len(word) else 0.0
        word_rms_db = 20.0 * np.log10(max(word_rms, 1e-10))

        # displaced speech: events outside the segment but close by
        near = [(a, b) for a, b in events if b > s - NEARBY and a < e + NEARBY]
        inside = [(a, b) for a, b in near if min(b, e) - max(a, s) > 0.03]
        outside = [(a, b) for a, b in near if min(b, e) - max(a, s) <= 0.03]
        displaced = 0.0
        for a, b in outside:
            gap = max(a - e, s - b, 0.0)
            if gap <= NEARBY:
                displaced = max(displaced, b - a)

        clip_frac = float(np.mean(np.abs(word) >= CLIP_LEVEL)) if len(word) else 0.0

        # word span vs. speech in the segment
        word_speech = span_db(inside, ws, we) if inside else 0.0
        word_frac = word_speech / max(we - ws, 1e-6)

        # edge truncation: the target word itself sits on a segment boundary
        edge_hit = (ws - s) < EDGE_MARGIN or (e - we) < EDGE_MARGIN

        reasons = []
        ratio = seg_peak / max(word_peak, 1e-9)
        if word_peak < SILENT_PEAK:
            if displaced > 0 or seg_peak > 0.03:
                verdict = "OFFSET"
                reasons.append("word span silent; speech nearby")
            else:
                verdict = "MISSING"
                reasons.append("no speech in span or nearby")
        elif word_peak < QUIET_PEAK:
            if (seg_peak > 0.05 and ratio >= 3.0) or word_frac < 0.4:
                verdict = "ALIGN"
                reasons.append(f"word span misses louder audio (seg/word={ratio:.1f}x)")
            elif displaced > 0:
                verdict = "OFFSET"
                reasons.append("quiet span; louder speech nearby")
            else:
                verdict = "QUIET"
                reasons.append("word span far below corpus level")
        else:
            verdict = "OK"
            if word_frac < 0.5:
                verdict = "ALIGN"
                reasons.append("word span covers <50% of segment speech")
        if clip_frac > 0.0005 and verdict == "OK":
            verdict = "CLIPPED"
            reasons.append("clipping in word span")
        if edge_hit and verdict == "OK":
            verdict = "TRIM"
            reasons.append("word span touches segment edge")

        rows.append(
            {
                "line": int(line["script_line"]),
                "diphone": line["token"],
                "source_file": fname,
                "source_start_s": round(s, 3),
                "source_end_s": round(e, 3),
                "word_start_s": round(ws, 3),
                "word_end_s": round(we, 3),
                "seg_peak": round(seg_peak, 5),
                "seg_speech_frac": round(seg_speech, 3),
                "word_peak": round(word_peak, 5),
                "word_rms_db": round(word_rms_db, 1),
                "word_speech_frac": round(word_frac, 3),
                "clip_frac": round(clip_frac, 5),
                "displaced_s": round(displaced, 3),
                "edge_hit": int(edge_hit),
                "verdict": verdict,
                "reason": ";".join(reasons),
            }
        )
    return rows


def main() -> int:
    global SEGMENTS, UNIT_GRID
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", default=str(OUT_DIR))
    parser.add_argument("--segments", default=str(SEGMENTS))
    parser.add_argument("--unit-grid", default=str(UNIT_GRID))
    args = parser.parse_args()
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    SEGMENTS = Path(args.segments)
    UNIT_GRID = Path(args.unit_grid)

    lines_by_file: dict[str, list[dict]] = defaultdict(list)
    for line in load_lines().values():
        lines_by_file[line["source_file"]].append(line)

    unit_grid = load_units()
    word_spans: dict[int, tuple[float, float]] = {}
    for u in unit_grid:
        ln = int(u["line"])
        ws, we = float(u["word_start_s"]), float(u["word_end_s"])
        if ln in word_spans:
            word_spans[ln] = (min(word_spans[ln][0], ws), max(word_spans[ln][1], we))
        else:
            word_spans[ln] = (ws, we)

    rows: list[dict] = []
    for fname, first, last in load_ranges():
        group = sorted(lines_by_file.get(fname, []), key=lambda r: int(r["script_line"]))
        if not group:
            continue
        print(f"auditing {fname} ({len(group)} lines)", flush=True)
        rows.extend(audit_file(fname, group, word_spans))

    rows.sort(key=lambda r: r["line"])
    fields = list(rows[0].keys())
    with (out_dir / "line_audit.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    verdict_by_line = {r["line"]: r["verdict"] for r in rows}
    units = load_units()
    unit_rows = []
    for u in units:
        verdict = verdict_by_line.get(int(u["line"]), "UNKNOWN")
        unit_rows.append(
            {
                "unit_id": u["unit_id"],
                "line": u["line"],
                "unit_type": u["unit_type"],
                "diphone": u["diphone"],
                "verdict": verdict,
                "source_wav": u["source_wav"],
            }
        )
    with (out_dir / "unit_audit.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(unit_rows[0].keys()))
        writer.writeheader()
        writer.writerows(unit_rows)

    counts = Counter(r["verdict"] for r in rows)
    unit_counts = Counter(r["verdict"] for r in unit_rows)
    peaks = [r["word_peak"] for r in rows if r["word_peak"] > 0]

    report = []
    report.append("Unit audio audit")
    report.append("=" * 40)
    report.append(f"Lines audited: {len(rows)}   Units: {len(unit_rows)}")
    report.append(f"Word-span peak: median={statistics.median(peaks):.4f}")
    report.append("")
    report.append("Line verdicts")
    for verdict, n in counts.most_common():
        report.append(f"  {verdict:8s} {n:5d} lines   {unit_counts.get(verdict, 0):5d} units")
    report.append("")
    report.append("Verdicts by source file")
    by_file = defaultdict(Counter)
    for r in rows:
        by_file[r["source_file"]][r["verdict"]] += 1
    for fname in sorted(by_file):
        parts = ", ".join(f"{k}={v}" for k, v in by_file[fname].most_common())
        report.append(f"  {fname}: {parts}")
    report.append("")
    report.append("Worst word-span peaks")
    for r in sorted(rows, key=lambda r: r["word_peak"])[:40]:
        report.append(
            f"  line {r['line']:4d} {r['diphone']:14s} peak={r['word_peak']:.4f} "
            f"disp={r['displaced_s']:.2f}s {r['verdict']:8s} {r['reason']}"
        )
    (out_dir / "report.txt").write_text("\n".join(report) + "\n", encoding="utf-8")

    for name, verdicts in (
        ("rerecord_lines.txt", {"MISSING", "CLIPPED"}),
        ("repair_lines.txt", {"OFFSET", "TRIM", "ALIGN"}),
        ("quiet_lines.txt", {"QUIET"}),
    ):
        chosen = [r for r in rows if r["verdict"] in verdicts]
        with (out_dir / name).open("w", encoding="utf-8") as handle:
            for r in chosen:
                handle.write(
                    f"{r['line']:4d}  {r['diphone']:14s} {r['verdict']:8s} "
                    f"peak={r['word_peak']:.4f} {r['reason']}\n"
                )

    print("\n".join(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
