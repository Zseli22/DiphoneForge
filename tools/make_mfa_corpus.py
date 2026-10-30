#!/usr/bin/env python3
"""
Create MFA corpus files from segmented utterances.

For each segmented WAV file, this writes a .lab file with the same basename
containing the transcript tokens (e.g. "say cv_p_ii").

Usage:
    python tools/make_mfa_corpus.py --segments-dir output/segments
"""

import argparse
import csv
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create MFA corpus .lab files.")
    parser.add_argument(
        "--segments-dir",
        type=Path,
        default=Path("output/segments"),
        help="Directory containing segmented WAV files and segments.csv.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    manifest = args.segments_dir / "segments.csv"
    if not manifest.exists():
        raise FileNotFoundError(manifest)

    count = 0
    with manifest.open("r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            wav_path = args.segments_dir / row["segment_file"]
            lab_path = wav_path.with_suffix(".lab")
            # Tokens are lower-case to match the dictionary entries.
            transcript = f"say {row['token']}"
            lab_path.write_text(transcript, encoding="utf-8")
            count += 1

    print(f"Wrote {count} .lab files in {args.segments_dir}")


if __name__ == "__main__":
    main()
