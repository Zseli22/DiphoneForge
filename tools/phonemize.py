#!/usr/bin/env python3
"""
Text-to-phoneme conversion using eSpeak-NG.

Calls the local eSpeak-NG binary with the -x flag to produce phoneme
names in eSpeak's General American symbol set, then normalises variant
symbols to the project's canonical inventory.

Usage:
    from tools.phonemize import phonemize
    phonemes = phonemize("hello world")
    # ['h', '@', 'l', 'oU', 'w', '3:', 'l', 'd']
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Set, Tuple

# Resolve paths relative to this file
_HERE = Path(__file__).resolve().parent
_PROJECT = _HERE.parent
_ESPEAK_BIN = _HERE / "espeak-ng" / "espeak-ng.exe"
_ESPEAK_DATA = _HERE / "espeak-ng"
_PHONEMES_JSON = _PROJECT / "data" / "phonemes.json"


def _load_variant_map() -> dict:
    """Load the eSpeak variant → canonical mapping from phonemes.json."""
    data = json.loads(_PHONEMES_JSON.read_text(encoding="utf-8"))
    variants = data.get("espeak_variants", {})
    variants.pop("_comment", None)
    return variants


# Ordered longest-first so multi-char variants match before shorter ones
_VARIANT_MAP = _load_variant_map()
_VARIANT_KEYS = sorted(_VARIANT_MAP.keys(), key=len, reverse=True)

# All known canonical phonemes (longest first for greedy matching)
_CANONICAL = set()
_data = json.loads(_PHONEMES_JSON.read_text(encoding="utf-8"))
for key in ("vowels", "consonants"):
    _CANONICAL.update(_data[key])
_CANONICAL_KEYS = sorted(_CANONICAL, key=len, reverse=True)


def _normalise_phoneme(sym: str) -> Optional[str]:
    """Map an eSpeak symbol to the canonical inventory, or None to drop it."""
    if sym in _CANONICAL:
        return sym
    if sym in _VARIANT_MAP:
        v = _VARIANT_MAP[sym]
        return v if v else None  # empty string means drop
    # Unknown symbol — pass through as-is (better than crashing)
    return sym


# All known phonemes (canonical + variants), longest first for greedy matching
_ALL_PHONEMES = sorted(
    _CANONICAL | set(_VARIANT_MAP.keys()),
    key=len,
    reverse=True,
)


def _tokenise_espeak(text: str) -> List[str]:
    """Split eSpeak -x output into individual phoneme symbols.

    eSpeak -x output looks like:  "h@l'oU w'3:ld"
    Stress markers (' and ˌ) are ignored.  Spaces mark word boundaries.
    Uses greedy longest-match against the known phoneme inventory.
    """
    tokens, _ = _tokenise_espeak_with_boundaries(text)
    return tokens


def _tokenise_espeak_with_boundaries(text: str) -> Tuple[List[str], Set[int]]:
    """Like _tokenise_espeak, but also returns word boundary positions.

    Returns (tokens, word_starts) where *word_starts* is a set of token
    indices at which a new word begins (i.e. the first phoneme after a
    space in the eSpeak output).
    """
    # Remove surrounding quotes and any [[word]] annotations
    text = re.sub(r'\[\[[^\]]*\]\]', '', text)
    text = text.strip().strip('"')

    tokens: List[str] = []
    word_starts: Set[int] = set()
    i = 0
    saw_space = False
    while i < len(text):
        ch = text[i]

        # Track word boundaries (spaces in the eSpeak output)
        if ch in (" ", "\t", "\r", "\n"):
            saw_space = True
            i += 1
            continue

        # Skip stress markers
        if ch in ("'", "ˌ"):
            i += 1
            continue

        # Greedy longest-match against known phonemes
        matched = False
        for ph in _ALL_PHONEMES:
            if text[i:i + len(ph)] == ph:
                if saw_space:
                    word_starts.add(len(tokens))
                    saw_space = False
                tokens.append(ph)
                i += len(ph)
                matched = True
                break

        if not matched:
            # Unknown character — skip it
            i += 1

    return tokens, word_starts


def phonemize(text: str, voice: str = "en-us") -> List[str]:
    """Convert text to a list of canonical phoneme symbols.

    Returns phonemes in the project's eSpeak General American inventory
    (e.g. ['h', '@', 'l', 'oU', 'w', '3:', 'l', 'd']).
    """
    phonemes, _ = phonemize_with_boundaries(text, voice)
    return phonemes


def phonemize_with_boundaries(
    text: str, voice: str = "en-us"
) -> Tuple[List[str], Set[int]]:
    """Convert text to phonemes, also returning word boundary positions.

    Returns (phonemes, word_starts) where *word_starts* is a set of
    phoneme indices at which a new word begins.  For example, "hello world"
    returns (['h','@','l','oU','w','3:','l','d'], {4}).
    """
    if not text.strip():
        return [], set()

    if not _ESPEAK_BIN.exists():
        raise FileNotFoundError(f"eSpeak-NG binary not found: {_ESPEAK_BIN}")

    env = os.environ.copy()
    env["ESPEAK_DATA_PATH"] = str(_ESPEAK_DATA)

    result = subprocess.run(
        [str(_ESPEAK_BIN), "-v", voice, "-q", "-x", text],
        capture_output=True,
        env=env,
    )
    raw = result.stdout.decode("utf-8", errors="replace")

    tokens, token_word_starts = _tokenise_espeak_with_boundaries(raw)
    phonemes: List[str] = []
    word_starts: Set[int] = set()
    for idx, tok in enumerate(tokens):
        canon = _normalise_phoneme(tok)
        if canon is not None:
            if idx in token_word_starts:
                word_starts.add(len(phonemes))
            phonemes.append(canon)

    return phonemes, word_starts


def phonemize_to_diphones(phonemes: List[str]) -> List[str]:
    """Convert a phoneme sequence to diphone labels.

    Consecutive phonemes are paired: ['h', '@', 'l', 'oU'] →
    ['h_@', '@_l', 'l_oU'].
    """
    diphones, _ = phonemize_to_diphones_with_boundaries(phonemes, set())
    return diphones


def phonemize_to_diphones_with_boundaries(
    phonemes: List[str], word_starts: Set[int]
) -> Tuple[List[str], Set[int]]:
    """Convert phonemes to diphones, tracking which cross word boundaries.

    Returns (diphones, boundary_diphone_indices) where
    *boundary_diphone_indices* contains the indices of diphones whose
    second phone starts a new word.  These are the diphone transitions
    where word-boundary splicing rules should apply.
    """
    diphones: List[str] = []
    boundary_indices: Set[int] = set()
    for i in range(len(phonemes) - 1):
        diphones.append(f"{phonemes[i]}_{phonemes[i+1]}")
        if (i + 1) in word_starts:
            boundary_indices.add(i)
    return diphones, boundary_indices


# --- CLI for testing ---
if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("text", nargs="+", help="Text to phonemize")
    args = parser.parse_args()

    text = " ".join(args.text)
    ph, ws = phonemize_with_boundaries(text)
    print(f"Text:      {text}")
    print(f"Phonemes:  {' '.join(ph)}")
    dips, bd = phonemize_to_diphones_with_boundaries(ph, ws)
    print(f"Diphones:  {' '.join(dips)}")
    if bd:
        labels = [dips[i] for i in sorted(bd)]
        print(f"Word boundaries at diphones: {', '.join(labels)}")
