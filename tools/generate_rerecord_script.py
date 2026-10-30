#!/usr/bin/env python3
"""
Generate a re-recording script from unit review results.

Reads the review progress and produces a targeted recording script
containing only the lines that need re-recording.

Usage:
    python tools/generate_rerecord_script.py
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
UNIT_GRID_PATH = PROJECT_ROOT / "output" / "unit_grid" / "unit_grid.json"
PROGRESS_PATH = PROJECT_ROOT / "output" / "unit_review" / "review_progress.json"
DIPHONE_MAP_PATH = PROJECT_ROOT / "output" / "diphone_map.json"
OUTPUT_DIR = PROJECT_ROOT / "output" / "unit_review"


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    if not PROGRESS_PATH.exists():
        print("No saved legacy unit-review progress was found; nothing to use for a pickup list.")
        return 1

    progress = load_json(PROGRESS_PATH)
    reviewed = progress["reviewed"]

    units = load_json(UNIT_GRID_PATH)["units"]
    diphone_map = load_json(DIPHONE_MAP_PATH)
    line_info = {entry["line"]: entry for entry in diphone_map["lines"]}

    rerecord = {
        uid: info
        for uid, info in reviewed.items()
        if info["verdict"] == "rerecord"
    }

    if not rerecord:
        print("No units marked for re-recording.")
        return 0

    unit_lookup = {u["unit_id"]: u for u in units}
    line_units: dict[int, list[dict]] = defaultdict(list)

    for uid in rerecord:
        unit = unit_lookup.get(uid)
        if unit:
            line_units[unit["line"]].append(
                {
                    "unit_id": uid,
                    "diphone": unit["diphone"],
                    "unit_type": unit["unit_type"],
                    "token": unit["token"],
                }
            )

    sorted_lines = sorted(line_units.keys())
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    script_lines = []
    aligner_lines = []
    dictionary: dict[str, list[str]] = {"say": ["s", "eI"]}
    all_diphones: set[str] = set()
    type_counts: dict[str, int] = defaultdict(int)

    for new_no, orig_line in enumerate(sorted_lines, 1):
        entry = line_info.get(orig_line)
        if not entry:
            continue

        phones_str = " ".join(entry["phones"])
        token = entry["token"]

        script_lines.append(
            f"{new_no:04d}. Say {phones_str}       # line {orig_line:04d}"
        )
        aligner_lines.append(f"{new_no:04d}. Say {token}")

        dictionary[token] = entry["phones"]

        for u in line_units[orig_line]:
            all_diphones.add(u["diphone"])
            type_counts[u["unit_type"]] += 1

    n_units = len(rerecord)
    n_lines = len(sorted_lines)
    header = (
        "# Re-recording script\n"
        f"# {n_units} units across {n_lines} lines need re-recording\n"
        f"# Original lines: {', '.join(f'{l:04d}' for l in sorted_lines)}\n"
        "#\n"
    )

    (OUTPUT_DIR / "rerecord_script.txt").write_text(
        header + "\n".join(script_lines) + "\n", encoding="utf-8"
    )
    (OUTPUT_DIR / "rerecord_aligner.txt").write_text(
        header + "\n".join(aligner_lines) + "\n", encoding="utf-8"
    )

    dict_lines = [
        f"{word}\t{' '.join(phones)}" for word, phones in dictionary.items()
    ]
    (OUTPUT_DIR / "rerecord_dictionary.txt").write_text(
        "# Token\tPhones\n" + "\n".join(dict_lines) + "\n", encoding="utf-8"
    )

    summary = [
        "Re-recording summary",
        "=" * 40,
        f"Total units reviewed: {len(reviewed)}",
        f"Units marked for re-recording: {n_units}",
        f"Lines to re-record: {n_lines}",
        "",
        "By type:",
    ]
    for t in ["CV", "VC", "CCV"]:
        if type_counts[t] > 0:
            summary.append(f"  {t}: {type_counts[t]} units")

    summary.append("")
    summary.append("Diphones to re-record:")
    for dp in sorted(all_diphones):
        count = sum(
            1 for ul in line_units.values() for uu in ul if uu["diphone"] == dp
        )
        summary.append(f"  {dp} ({count} unit{'s' if count > 1 else ''})")

    summary.append("")
    summary.append("Lines to re-record:")
    for line_no in sorted_lines:
        entry = line_info.get(line_no)
        units_str = ", ".join(u["diphone"] for u in line_units[line_no])
        if entry:
            phones_str = " ".join(entry["phones"])
            summary.append(f"  {line_no:04d}. Say {phones_str}  [{units_str}]")

    (OUTPUT_DIR / "rerecord_summary.txt").write_text(
        "\n".join(summary) + "\n", encoding="utf-8"
    )

    print(f"Generated re-recording script: {n_units} units, {n_lines} lines")
    print(f"Files in {OUTPUT_DIR}:")
    print("  rerecord_script.txt")
    print("  rerecord_aligner.txt")
    print("  rerecord_dictionary.txt")
    print("  rerecord_summary.txt")
    return 0


if __name__ == "__main__":
    sys.exit(main())
