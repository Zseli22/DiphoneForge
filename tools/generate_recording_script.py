#!/usr/bin/env python3
"""
Generate a recording script and supporting files for a diphone voice database.

The script creates nonsense-word prompts that cover the core diphone units needed
for US English: CV, VC, and up to two-consonant CCV onsets (and optional VCC codas).
By default, phonotactically impossible or redundant units are omitted so the first
recording session is shorter. Each prompt is wrapped in a carrier phrase to encourage
consistent rate and loudness.

The phone inventory follows eSpeak-NG General American symbols. Aligner tokens
are safe ASCII equivalents defined in data/phonemes.json.

Outputs:
    recording_script.txt      Human-readable prompts to read during recording.
    aligner_transcript.txt    Same prompts but with machine tokens for forced alignment.
    dictionary.txt            Token-to-phone dictionary for the aligner.
    diphone_map.json          Maps each recorded line to the diphone(s) it contains.
    metadata.json             Summary counts and settings.

Usage:
    python tools/generate_recording_script.py --vowel-set full --output-dir output
"""

import argparse
import json
import random
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent


def load_json(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate a diphone recording script for US English."
    )
    parser.add_argument(
        "--vowel-set",
        choices=["core", "full"],
        default="full",
        help="Which vowel set to use. 'core' uses a minimal eSpeak vowel set, 'full' uses all eSpeak US English vowels.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=PROJECT_ROOT / "output",
        help="Directory where the generated files will be written.",
    )
    parser.add_argument(
        "--carrier",
        default='Say {target}',
        help="Carrier phrase. Use {target} where the diphone prompt should appear. "
             "If you change this, update the dictionary entries in the script accordingly.",
    )
    parser.add_argument(
        "--display-words",
        type=Path,
        default=None,
        help="Optional JSON file mapping token IDs to friendlier display words for the speaker. "
             "The aligner still uses the phonetic token. The display word should match the target phones.",
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=1,
        help="How many times to repeat each item.",
    )
    parser.add_argument(
        "--shuffle",
        action="store_true",
        help="Randomize the order of prompts to avoid phonetic context fatigue.",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=42,
        help="Random seed for --shuffle.",
    )
    parser.add_argument(
        "--codas",
        action="store_true",
        help="Include VCC (vowel + coda cluster) targets for syllable-final consonant clusters.",
    )
    parser.add_argument(
        "--coda-clusters",
        type=Path,
        default=PROJECT_ROOT / "data" / "coda_clusters.json",
        help="JSON file listing coda clusters to use when --codas is set.",
    )
    parser.add_argument(
        "--coda-vowel",
        default="A:",
        help="Vowel used for the single VCC prompt per coda cluster (default: A:).",
    )
    parser.add_argument(
        "--coda-all-vowels",
        action="store_true",
        help="Generate a VCC prompt for every vowel (large; usually unnecessary).",
    )
    parser.add_argument(
        "--no-phonotactic-cleanup",
        action="store_true",
        help="Disable phonotactic cleanup and generate the full Cartesian inventory. "
             "By default, impossible/redundant units (e.g., /N/ onsets, /h w j/ codas, "
             "V+V diphones, and V+/r/ units covered by r-colored vowels) are omitted.",
    )
    return parser.parse_args()


def make_token(unit_type: str, phones: list[str], token_names: dict[str, str]) -> str:
    """Create a single-token name for forced alignment from safe phone names."""
    safe = [token_names.get(p, p) for p in phones]
    joined = "_".join(safe)
    return f"{unit_type}_{joined}".lower()


def make_speaker_target(phones: list[str]) -> str:
    """Return the phone sequence as the speaker will read it."""
    return " ".join(phones)


# Vowels whose coda /r/ can be represented by an r-colored vowel already in the
# inventory. When phonotactic cleanup is enabled, VC units like "A: r" are omitted
# because "A@" already covers the same transition.
R_COLORED_REPLACEMENTS = {
    "A:": "A@",
    "O": "O@",
    "E": "e@",
    "I": "i@",
    "U": "U@",
    "@": "3",
    "3": "3:",
}

# R-colored vowels themselves; a following /r/ coda would be redundant.
R_COLORED_VOWELS = {"A@", "O@", "e@", "i@", "U@", "3:"}


def build_targets(
    vowels: list[str],
    consonants: list[str],
    clusters: list[list[str]],
    token_names: dict[str, str],
    coda_clusters: list[list[str]] | None = None,
    phonotactic_cleanup: bool = True,
    coda_vowel: str = "A:",
    coda_all_vowels: bool = False,
):
    """Build the list of diphone targets to record."""
    targets = []

    # CV: consonant + vowel
    for c in consonants:
        if phonotactic_cleanup and c == "N":
            # /N/ (ng) cannot appear in syllable onset in English.
            continue
        for v in vowels:
            phones = [c, v]
            targets.append({
                "unit_type": "CV",
                "token": make_token("CV", phones, token_names),
                "phones": phones,
                "diphones": [[c, v]],
            })

    # VC: vowel + consonant
    for v in vowels:
        for c in consonants:
            if phonotactic_cleanup:
                if c in {"h", "w", "j"}:
                    # /h/, /w/, /j/ do not occur as syllable codas.
                    continue
                if c == "r" and (v in R_COLORED_REPLACEMENTS or v in R_COLORED_VOWELS):
                    # Use the r-colored vowel (e.g. A@) instead of V+r.
                    continue
            phones = [v, c]
            targets.append({
                "unit_type": "VC",
                "token": make_token("VC", phones, token_names),
                "phones": phones,
                "diphones": [[v, c]],
            })

    # VV: vowel + vowel
    if not phonotactic_cleanup:
        for v1 in vowels:
            for v2 in vowels:
                phones = [v1, v2]
                targets.append({
                    "unit_type": "VV",
                    "token": make_token("VV", phones, token_names),
                    "phones": phones,
                    "diphones": [[v1, v2]],
                })

    # CCV: two-consonant onset + vowel
    for cluster in clusters:
        c1, c2 = cluster[0], cluster[1]
        for v in vowels:
            phones = [c1, c2, v]
            targets.append({
                "unit_type": "CCV",
                "token": make_token("CCV", phones, token_names),
                "phones": phones,
                "diphones": [[c1, c2], [c2, v]],
            })

    # VCC: vowel + two-consonant coda (captures coda CC diphones)
    # Only the C1+C2 diphone is new; the V+C1 half duplicates the VC inventory,
    # so one prompt per cluster is enough unless --coda-all-vowels is requested.
    if coda_clusters:
        coda_vowels = vowels if coda_all_vowels else [coda_vowel]
        for cluster in coda_clusters:
            c1, c2 = cluster[0], cluster[1]
            for v in coda_vowels:
                phones = [v, c1, c2]
                targets.append({
                    "unit_type": "VCC",
                    "token": make_token("VCC", phones, token_names),
                    "phones": phones,
                    "diphones": [[v, c1], [c1, c2]],
                })

    return targets


def main() -> None:
    args = parse_args()

    phonemes = load_json(PROJECT_ROOT / "data" / "phonemes.json")
    clusters = load_json(PROJECT_ROOT / "data" / "onset_clusters.json")
    coda_clusters = None
    if args.codas:
        coda_clusters = load_json(args.coda_clusters)["clusters"]

    vowels = phonemes["vowels_core"] if args.vowel_set == "core" else phonemes["vowels"]
    consonants = phonemes["consonants"]
    cluster_list = clusters["clusters"]
    token_names = phonemes["token_names"]

    targets = build_targets(
        vowels,
        consonants,
        cluster_list,
        token_names,
        coda_clusters,
        phonotactic_cleanup=not args.no_phonotactic_cleanup,
        coda_vowel=args.coda_vowel,
        coda_all_vowels=args.coda_all_vowels,
    )

    if args.shuffle:
        rng = random.Random(args.seed)
        rng.shuffle(targets)

    args.output_dir.mkdir(parents=True, exist_ok=True)

    display_words = {}
    if args.display_words:
        display_words = load_json(args.display_words)

    recording_lines = []
    aligner_lines = []
    diphone_map = []
    dictionary = {}

    # Carrier words for the aligner dictionary, in eSpeak notation.
    # If you change --carrier, add the new carrier words here.
    dictionary["say"] = ["s", "eI"]

    line_no = 1
    for target in targets:
        speaker_target = display_words.get(target["token"], make_speaker_target(target["phones"]))
        aligner_target = target["token"]

        recording_line = args.carrier.format(target=speaker_target)
        aligner_line = args.carrier.format(target=aligner_target).lower()

        # Token-to-phone dictionary entry
        dictionary[target["token"]] = target["phones"]

        for _ in range(args.repeats):
            recording_lines.append(f"{line_no:04d}. {recording_line}")
            aligner_lines.append(f"{line_no:04d}. {aligner_line}")
            diphone_map.append({
                "line": line_no,
                "unit_type": target["unit_type"],
                "token": target["token"],
                "phones": target["phones"],
                "diphones": target["diphones"],
            })
            line_no += 1

    header = (
        "# Diphone recording script\n"
        f"# Vowel set: {args.vowel_set} ({len(vowels)} vowels)\n"
        f"# Consonants: {len(consonants)}\n"
        f"# CCV clusters: {len(cluster_list)}\n"
        f"# Total unique targets: {len(targets)}\n"
        f"# Repeats per target: {args.repeats}\n"
        "# Read each line naturally and consistently.\n"
    )

    (args.output_dir / "recording_script.txt").write_text(
        header + "\n".join(recording_lines) + "\n", encoding="utf-8"
    )
    (args.output_dir / "aligner_transcript.txt").write_text(
        header + "\n".join(aligner_lines) + "\n", encoding="utf-8"
    )

    dict_lines = [f"{word}\t{' '.join(phones)}" for word, phones in dictionary.items()]
    (args.output_dir / "dictionary.txt").write_text(
        "# Token\tPhones\n" + "\n".join(dict_lines) + "\n", encoding="utf-8"
    )

    (args.output_dir / "diphone_map.json").write_text(
        json.dumps({
            "metadata": {
                "vowel_set": args.vowel_set,
                "num_vowels": len(vowels),
                "num_consonants": len(consonants),
                "num_clusters": len(cluster_list),
                "num_unique_targets": len(targets),
                "repeats": args.repeats,
                "shuffled": args.shuffle,
                "seed": args.seed if args.shuffle else None,
                "carrier": args.carrier,
            },
            "lines": diphone_map,
        }, indent=2),
        encoding="utf-8",
    )

    # Simple metadata summary
    metadata = {
        "vowel_set": args.vowel_set,
        "vowels": vowels,
        "num_consonants": len(consonants),
        "num_clusters": len(cluster_list),
        "num_unique_targets": len(targets),
        "num_recorded_lines": len(recording_lines),
        "repeats": args.repeats,
        "carrier": args.carrier,
    }
    (args.output_dir / "metadata.json").write_text(
        json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
    )

    print(f"Generated {len(recording_lines)} recording lines in {args.output_dir}")
    print(f"Unique diphone targets: {len(targets)}")
    print("Files created:")
    for name in ["recording_script.txt", "aligner_transcript.txt", "dictionary.txt", "diphone_map.json", "metadata.json"]:
        print(f"  - {name}")


if __name__ == "__main__":
    main()
