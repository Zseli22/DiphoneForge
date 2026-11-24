#!/usr/bin/env python3
"""Language-neutral hybrid real-word + minimal-pair diphone corpus planner.

Uses eSpeak NG (-x --sep=_ and --ipa --sep=_) together with a plain-text
frequency word list to:
  1. Discover a language's actual emitted phone inventory and IPA mapping.
  2. Measure real-text diphone frequencies across the vocabulary.
  3. Select a compact, frequency-weighted greedy set-cover of short,
     easy-to-read real words that maximize real-speech diphone coverage
     (preferring stressed/word-medial occurrences over reduced word-final tails).
  4. Supplement any remaining uncovered core CV/VC pairs with a small
     minimal-pair tail at the end.

Zero third-party Python dependencies (standard library + local espeak-ng).

Usage:
    python tools/plan_word_corpus.py --voice en-gb-x-rp --wordlist output/wordlists/en_50k.txt --out-dir output/en_gb_plan
"""

from __future__ import annotations

import argparse
import collections
import csv
import json
import math
import os
import re
import subprocess
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Set, Tuple


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DEFAULT_ESPEAK_BIN = SCRIPT_DIR / "espeak-ng" / "espeak-ng.exe"
DEFAULT_ESPEAK_DATA = SCRIPT_DIR / "espeak-ng"

# Common English heteronyms / homographs whose spelling does not uniquely
# specify pronunciation in isolation. Avoided as recording prompts unless needed.
ENGLISH_HETEROGRAPHS: Set[str] = {
    "read", "lead", "live", "wind", "tear", "bow", "row", "close", "minute",
    "bass", "wound", "polish", "desert", "object", "present", "record",
    "refuse", "produce", "project", "content", "contract", "invalid",
    "console", "entrance", "excuse", "house", "use", "abuse", "sow", "dove",
    "polish", "resume", "rebel", "permit", "suspect", "digest", "attribute",
    "conduct", "conflict", "contest", "convert", "convict", "decrease",
    "defect", "escort", "export", "extract", "import", "increase", "insult",
    "insert", "moderate", "perfect", "proceed", "progress", "protest",
    "refund", "reject", "separate", "subject", "survey", "transfer",
    "transport", "upset", "alternate", "advocate", "appropriate", "articulate",
    "associate", "coordinate", "deliberate", "duplicate", "elaborate",
    "estimate", "graduate", "intimate", "legitimate", "predicate", "tearful",
    "moped", "sewer", "slough", "agape", "axes", "buffet", "drawer", "gill",
    "learned", "mow", "number", "pasty", "primer", "putting", "resign",
    "router", "skied", "unionized", "wicked", "blessed", "aged", "dogged",
    "ragged", "jagged", "crooked", "learned",
}

# Coarse profanity / subtitle junk filter so recording scripts stay pleasant.
ENGLISH_SKIP_WORDS: Set[str] = {
    "fuck", "fucking", "fucked", "fucker", "motherfucker", "shit", "shitty",
    "bullshit", "bitch", "bitches", "bastard", "asshole", "dick", "cock",
    "cunt", "pussy", "whore", "slut", "nigger", "nigga", "faggot", "retard",
    "piss", "crap", "damn", "goddamn", "hell", "wanker", "bollocks", "twat",
    "prick", "arse", "arsehole", "shag", "bugger",
    # Subtitle fillers / interjections / abbreviations
    "uh", "um", "hm", "hmm", "uhh", "umm", "ahh", "aah", "aaah", "ooh", "ohh",
    "aha", "huh", "meh", "nah", "yeah", "yep", "yup", "nope", "okay", "ok",
    "hey", "yo", "ow", "ew", "ugh", "phew", "shh", "psst", "whoa", "woah",
    "haha", "hahaha", "hehe", "lol", "omg", "wtf", "btw", "idk", "tbh",
    "tv", "mr", "mrs", "ms", "dr", "st", "jr", "sr", "vs", "etc", "eg", "ie",
    "ll", "ve", "re", "em", "er", "la", "le", "de", "el", "al", "ya", "ye",
    "ma", "pa", "da", "ha", "ho", "hi", "eh", "oh", "ah",
}

# Built-in UK English (non-rhotic RP / GB) normalization preset.
# Matches eSpeak NG's en-gb / en-gb-x-rp and en1_phtrans collapses.
EN_GB_VOWELS: List[str] = [
    "i:", "I", "E", "a", "A:", "0", "O:", "V", "U", "u:", "@", "3:",
    "eI", "aI", "OI", "aU", "oU", "i@", "e@", "U@",
]
EN_GB_CONSONANTS: List[str] = [
    "p", "t", "k", "b", "d", "g",
    "tS", "dZ",
    "f", "v", "T", "D", "s", "z", "S", "Z", "h",
    "m", "n", "N",
    "l", "L", "r", "w", "j",
]

# Multi-phone expansions (mirroring phtrans two-phone splits) and allophone collapses.
EN_GB_NORM_BASE: Dict[str, Tuple[str, ...]] = {
    "3": ("@",),
    "A@": ("A:",),
    "O@": ("O:",),
    "o@": ("O:",),
    "O": ("O:",),
    "i@3": ("i@",),
    "IR": ("3:",),
    "VR": ("3:",),
    "I2": ("I",),
    "I#": ("I",),
    "i": ("i:",),          # modern Southern British happy-tensing
    "a#": ("@",),
    "@2": ("@",),
    "@5": ("@",),
    "@/": ("@",),
    "@-": (),              # optional schwa dropped in en1_phtrans
    "@L": ("@", "L"),      # syllabic dark l: table, people
    "aI@": ("aI", "@"),    # fire, tired (non-rhotic)
    "aI3": ("aI", "@"),
    "aU@": ("aU", "@"),    # hour, flour (non-rhotic)
    "aU3": ("aU", "@"),
    "aI2": ("aI",),
    "a/": ("a",),
    "a:": ("A:",),
    "E#": ("E",),
    "e#": ("E",),
    "e": ("E",),
    "o": ("O:",),
    "0#": ("0",),
    "02": ("V",),
    "oU#": ("oU",),
    "A#": ("A:",),
    "r-": ("r",),
    "r/": ("r",),
    "w/": ("w",),
    "w#": ("w",),
    "l/2": ("L",),
    "l/": ("L",),
    "l/3": ("L",),
    "l^": ("l",),
    "l.": ("L",),
    "l-": ("L",),
    "L/": ("L",),
    "t2": ("t",),
    "t#": ("t",),
    "d#": ("d",),
    "n-": ("n",),
    "m-": ("m",),
    "N-": ("N",),
    "x": ("k",),
    ";": (),
    "g-": (),
    "?": (),
    "|": (),
    "!": (),
}

EN_GB_IPA_DEFAULTS: Dict[str, str] = {
    "i:": "iː", "I": "ɪ", "E": "ɛ", "a": "a", "A:": "ɑː", "0": "ɒ", "O:": "ɔː",
    "V": "ʌ", "U": "ʊ", "u:": "uː", "@": "ə", "3:": "ɜː",
    "eI": "eɪ", "aI": "aɪ", "OI": "ɔɪ", "aU": "aʊ", "oU": "əʊ",
    "i@": "ɪə", "e@": "eə", "U@": "ʊə",
    "p": "p", "t": "t", "k": "k", "b": "b", "d": "d", "g": "ɡ",
    "tS": "tʃ", "dZ": "dʒ",
    "f": "f", "v": "v", "T": "θ", "D": "ð", "s": "s", "z": "z",
    "S": "ʃ", "Z": "ʒ", "h": "h",
    "m": "m", "n": "n", "N": "ŋ",
    "l": "l", "L": "ɫ", "r": "ɹ", "w": "w", "j": "j",
}


@dataclass
class WordEntry:
    word: str
    freq: int
    rank: int
    phones: Tuple[str, ...]
    stresses: Tuple[int, ...]     # 1=primary, 2=secondary, 0=unstressed
    ipa_phones: Tuple[str, ...]
    raw_espeak: str
    diphones: Dict[str, float]    # diphone -> best positional quality score in this word


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--voice", default="en-gb",
        help="eSpeak NG voice code (e.g. en-gb, en-gb-x-rp, en-us, hu, es, hr).",
    )
    parser.add_argument(
        "--compare-voice", default="auto",
        help="Optional companion dialect voice used to automatically skip words whose "
             "pronunciation varies across dialects (default: auto -> compares en-gb vs en-gb-x-rp). "
             "Use 'none' to disable.",
    )
    parser.add_argument(
        "--skip-symbols", default="auto",
        help="Comma-separated raw eSpeak symbols to exclude from candidate words "
             "(default: auto -> skips 'aa' BATH words in en-gb). Use 'none' to disable.",
    )
    parser.add_argument(
        "--wordlist", type=Path, required=True,
        help="Plain-text word list: 'word' or 'word count' per line.",
    )
    parser.add_argument(
        "--out-dir", type=Path, default=PROJECT_ROOT / "output" / "en_gb_plan",
        help="Output directory for plan reports, script, and diphone map.",
    )
    parser.add_argument(
        "--max-vocab", type=int, default=20000,
        help="Top N clean words from wordlist used to model real-text diphones (default: 20000).",
    )
    parser.add_argument(
        "--candidate-vocab", type=int, default=15000,
        help="Top N clean words eligible as recording prompts (default: 15000; keeps words familiar).",
    )
    parser.add_argument(
        "--min-len", type=int, default=2,
        help="Minimum word character length for candidate prompts (default: 2).",
    )
    parser.add_argument(
        "--max-len", type=int, default=11,
        help="Maximum word character length for candidate prompts (default: 11).",
    )
    parser.add_argument(
        "--min-diphone-words", type=int, default=2,
        help="Minimum distinct words in vocab containing a diphone to include it in the real-text target set (default: 2).",
    )
    parser.add_argument(
        "--target-word-coverage", type=float, default=0.95,
        help="Target fraction of real-text diphones to cover with real words before stopping (default: 0.95).",
    )
    parser.add_argument(
        "--max-words", type=int, default=500,
        help="Hard cap on selected real words (default: 500).",
    )
    parser.add_argument(
        "--min-takes", type=int, default=1,
        help="Target minimum takes per real-text diphone in greedy word cover (1 = compact, 2 = redundant 2x coverage).",
    )
    parser.add_argument(
        "--extend-from", type=Path, default=None,
        help="Optional existing plan dir (diphone_map.json) whose recorded lines are counted as already covered; "
             "selects ONLY additional new words to append as an extension session.",
    )
    parser.add_argument(
        "--bath", choices=["auto", "A:", "a"], default="auto",
        help="How to map eSpeak 'aa' (BATH vowel) in en-gb: auto (A: for en-gb-x-rp, a for en-gb), A:, or a.",
    )
    parser.add_argument(
        "--supplement-mode", choices=["real-gaps", "cv-vc"], default="real-gaps",
        help="What the minimal-pair supplement tail targets: 'real-gaps' (default: uncovered real-text diphones occurring in vocab) or 'cv-vc' (full theoretical CV/VC matrix).",
    )
    parser.add_argument(
        "--espeak-bin", type=Path, default=DEFAULT_ESPEAK_BIN,
        help="Path to espeak-ng executable.",
    )
    parser.add_argument(
        "--espeak-data", type=Path, default=DEFAULT_ESPEAK_DATA,
        help="Path to directory containing espeak-ng-data.",
    )
    return parser.parse_args()


def is_clean_word(word: str, min_len: int, max_len: int, ascii_only: bool) -> bool:
    if not (min_len <= len(word) <= max_len):
        return False
    if ascii_only:
        return bool(re.fullmatch(r"[a-z]+", word))
    return all(ch.isalpha() and ch.islower() for ch in word)


def load_wordlist(path: Path, max_vocab: int, ascii_only: bool) -> List[Tuple[str, int]]:
    words: List[Tuple[str, int]] = []
    seen: Set[str] = set()
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for idx, raw in enumerate(f):
            parts = raw.strip().split()
            if not parts:
                continue
            w = parts[0].lower()
            if w in seen or not is_clean_word(w, 2, 16, ascii_only):
                continue
            freq = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else max(1, 100000 - idx)
            seen.add(w)
            words.append((w, freq))
            if len(words) >= max_vocab:
                break
    return words


def batch_phonemize(
    words: Sequence[str],
    voice: str,
    espeak_bin: Path,
    espeak_data: Path,
    chunk_size: int = 4000,
) -> Tuple[List[str], List[str]]:
    env = os.environ.copy()
    env["ESPEAK_DATA_PATH"] = str(espeak_data.resolve())
    hidden_kw: Dict[str, object] = {}
    if os.name == "nt":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
        hidden_kw["startupinfo"] = si
        hidden_kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    x_lines: List[str] = []
    ipa_lines: List[str] = []
    for i in range(0, len(words), chunk_size):
        batch = "\n".join(words[i:i + chunk_size]) + "\n"
        rx = subprocess.run(
            [str(espeak_bin), "-v", voice, "-q", "-x", "--sep=_"],
            input=batch.encode("utf-8"),
            capture_output=True,
            env=env,
            check=True,
            **hidden_kw,
        )
        ri = subprocess.run(
            [str(espeak_bin), "-v", voice, "-q", "--ipa", "--sep=_"],
            input=batch.encode("utf-8"),
            capture_output=True,
            env=env,
            check=True,
            **hidden_kw,
        )
        x_lines.extend(rx.stdout.decode("utf-8", "replace").splitlines())
        ipa_lines.extend(ri.stdout.decode("utf-8", "replace").splitlines())
    return x_lines, ipa_lines


def get_normalization(voice: str, bath: str) -> Tuple[Optional[Set[str]], Optional[Set[str]], Dict[str, Tuple[str, ...]]]:
    vlow = voice.lower()
    if vlow.startswith("en-gb") or vlow == "en":
        norm = dict(EN_GB_NORM_BASE)
        if bath == "auto":
            norm["aa"] = ("A:",) if "rp" in vlow else ("a",)
        else:
            norm["aa"] = (bath,)
        return set(EN_GB_VOWELS), set(EN_GB_CONSONANTS), norm
    return None, None, {";": (), "|": (), "!": (), "?": (), "@-": (), "*": ()}


def normalize_word_phones(
    x_line: str,
    ipa_line: str,
    norm_map: Dict[str, Tuple[str, ...]],
    canonical: Optional[Set[str]],
) -> Optional[Tuple[Tuple[str, ...], Tuple[int, ...], Tuple[str, ...]]]:
    x_line = x_line.strip()
    ipa_line = ipa_line.strip()
    # Skip empty, multi-word, phoneme-escaped, or language-switched (e.g. "(en)...(es)") words
    if not x_line or " " in x_line or "[[" in x_line or "(" in x_line:
        return None

    raw_x = [t for t in x_line.split("_") if t]
    raw_ipa = [t for t in ipa_line.split("_") if t]
    ipa_aligned = len(raw_x) == len(raw_ipa)

    phones: List[str] = []
    stresses: List[int] = []
    ipas: List[str] = []

    for idx, tok in enumerate(raw_x):
        stress = 0
        if tok.startswith("'"):
            stress = 1
            sym = tok.lstrip("',")
        elif tok.startswith(","):
            stress = 2
            sym = tok.lstrip("',")
        else:
            sym = tok
        if not sym:
            continue

        ipa_tok = raw_ipa[idx].lstrip("ˈˌ") if ipa_aligned else ""
        if sym in norm_map:
            mapped = norm_map[sym]
            for j, m in enumerate(mapped):
                phones.append(m)
                stresses.append(stress if j == 0 else 0)
                ipas.append(EN_GB_IPA_DEFAULTS.get(m, ipa_tok) if canonical is not None else ipa_tok)
        else:
            if canonical is not None and sym not in canonical:
                return None  # skip word with unexpected foreign/broken symbol
            phones.append(sym)
            stresses.append(stress)
            ipas.append(EN_GB_IPA_DEFAULTS.get(sym, ipa_tok) if canonical is not None else ipa_tok)

    if len(phones) < 2:
        return None
    # In British English (when both clear 'l' and dark 'L' are in the canonical inventory),
    # mirror eSpeak NG's internal `l -> l/2` coda rule: 'l' before a non-vowel or word end -> 'L'.
    if canonical is not None and "l" in canonical and "L" in canonical:
        v_set = set(EN_GB_VOWELS)
        for i in range(len(phones)):
            if phones[i] == "l" and (i == len(phones) - 1 or phones[i + 1] not in v_set):
                phones[i] = "L"
                ipas[i] = "ɫ"
    return tuple(phones), tuple(stresses), tuple(ipas)


def extract_diphones_with_quality(
    phones: Tuple[str, ...],
    stresses: Tuple[int, ...],
) -> Dict[str, float]:
    """Return diphone -> best positional quality multiplier inside a single word.

    Prefers stressed and word-medial diphones (steady voicing, clean articulation,
    avoiding utterance-final trailing off / vocal fry).
    """
    n = len(phones)
    out: Dict[str, float] = {}

    # Word edges (_X and X_)
    out[f"_{phones[0]}"] = 1.0
    out[f"{phones[-1]}_"] = 1.0

    for i in range(n - 1):
        dp = f"{phones[i]}_{phones[i + 1]}"
        is_stressed = stresses[i] == 1 or stresses[i + 1] == 1
        is_medial = (0 < i) and (i + 1 < n - 1)
        is_final = (i + 1 == n - 1)
        q = 1.0
        if is_stressed:
            q += 0.25
        if is_medial:
            q += 0.15
        elif is_final and not is_stressed:
            q -= 0.15
        if dp not in out or q > out[dp]:
            out[dp] = q
    return out


def is_good_prompt_candidate(
    entry: WordEntry,
    min_len: int,
    max_len: int,
    max_rank: int,
    is_english: bool,
    vowel_set: Set[str],
) -> bool:
    if entry.rank > max_rank:
        return False
    if not (min_len <= len(entry.word) <= max_len):
        return False
    # Must contain at least one vowel (filters stray consonant-only acronyms)
    if vowel_set and not any(p in vowel_set for p in entry.phones):
        return False
    if is_english:
        if entry.word in ENGLISH_SKIP_WORDS or entry.word in ENGLISH_HETEROGRAPHS:
            return False
        # Require an orthographic vowel letter (eliminates consonant acronyms like bmw, fbi, wh, pr)
        if not re.search(r"[aeiouy]", entry.word):
            return False
        # Reject spelled-out acronyms where eSpeak expands letters into far more phones than chars
        if len(entry.phones) > len(entry.word) + 1:
            return False
        if ";" in entry.raw_espeak and entry.stresses.count(2) >= 2:
            return False
        # Avoid awkward 3+ identical letters (e.g. "shhh", "aaah")
        if re.search(r"(.)\1\1", entry.word):
            return False
    return True


def greedy_set_cover(
    candidates: List[WordEntry],
    target_diphones: Set[str],
    diphone_freq: Dict[str, int],
    max_words: int,
    target_coverage: float,
    min_takes: int = 1,
    initial_counts: Optional[Dict[str, int]] = None,
    exclude_words: Optional[Set[str]] = None,
) -> Tuple[List[WordEntry], List[Dict[str, object]]]:
    """Frequency-weighted greedy set cover supporting k>=1 takes per diphone and additive extensions."""
    weights = {dp: math.log1p(diphone_freq.get(dp, 1)) ** 1.3 for dp in target_diphones}
    total_target_count = len(target_diphones)
    total_target_freq = sum(diphone_freq.get(dp, 0) for dp in target_diphones)
    excl = exclude_words or set()

    # Pre-filter each candidate's diphones to the target set & count how many candidates have each diphone
    cand_targets: List[List[Tuple[str, float]]] = []
    cand_cost: List[float] = []
    avail_counts: collections.Counter[str] = collections.Counter(initial_counts or {})
    for c in candidates:
        hits = [(dp, q) for dp, q in c.diphones.items() if dp in target_diphones]
        cand_targets.append(hits)
        rank_penalty = 1.0 + 0.12 * math.log1p(c.rank / 500.0)
        len_penalty = 1.0 + 0.04 * max(0, len(c.word) - 7)
        cand_cost.append(rank_penalty * len_penalty)
        if c.word not in excl:
            for dp, _ in hits:
                avail_counts[dp] += 1

    need: Dict[str, int] = {dp: min(max(1, min_takes), max(1, avail_counts[dp])) for dp in target_diphones}
    have: collections.Counter[str] = collections.Counter(initial_counts or {})
    covered_1: Set[str] = {dp for dp in target_diphones if have[dp] >= 1}
    covered_k: Set[str] = {dp for dp in target_diphones if have[dp] >= need[dp]}
    covered_freq = sum(diphone_freq.get(dp, 0) for dp in covered_1)
    selected: List[WordEntry] = []
    used_idx: Set[int] = {i for i, c in enumerate(candidates) if c.word in excl}
    curve: List[Dict[str, object]] = []

    while len(selected) < max_words:
        best_i = -1
        best_score = 0.0
        best_new: List[str] = []

        for i, hits in enumerate(cand_targets):
            if i in used_idx or not hits:
                continue
            gain = 0.0
            new_dps: List[str] = []
            for dp, q in hits:
                cur_n = have[dp]
                if cur_n < need[dp]:
                    # First take weighted 1.0; subsequent redundancy takes weighted 0.65
                    mult = 1.0 if cur_n == 0 else (0.65 ** cur_n)
                    gain += weights[dp] * q * mult
                    new_dps.append(dp)
            if not new_dps:
                continue
            score = gain / cand_cost[i]
            if score > best_score:
                best_score = score
                best_i = i
                best_new = new_dps

        if best_i < 0:
            break

        used_idx.add(best_i)
        entry = candidates[best_i]
        selected.append(entry)
        for dp in best_new:
            was_zero = have[dp] == 0
            have[dp] += 1
            if was_zero:
                covered_1.add(dp)
                covered_freq += diphone_freq.get(dp, 0)
            if have[dp] >= need[dp]:
                covered_k.add(dp)

        frac_types = (len(covered_k) if min_takes > 1 else len(covered_1)) / total_target_count if total_target_count else 1.0
        frac_tokens = covered_freq / total_target_freq if total_target_freq else 1.0
        curve.append({
            "step": len(selected),
            "word": entry.word,
            "rank": entry.rank,
            "phones": " ".join(entry.phones),
            "new_diphones": len(best_new),
            "cum_diphones": len(covered_1),
            "type_coverage": round(frac_types, 5),
            "token_coverage": round(frac_tokens, 5),
            "added": " ".join(sorted(best_new)),
        })

        if frac_types >= target_coverage:
            break

    return selected, curve


def build_supplement(
    covered_diphones: Set[str],
    target_diphones: Set[str],
    diphone_freq: Dict[str, int],
    vowels: List[str],
    consonants: List[str],
    voice: str,
    mode: str = "real-gaps",
) -> List[Dict[str, object]]:
    """Build a compact minimal-pair supplement tail for uncovered diphones."""
    if mode == "real-gaps":
        # Only supplement internal diphones that actually occur in real text
        # and were missed by the selected real words.
        uncovered = sorted(
            (
                dp for dp in (target_diphones - covered_diphones)
                if not dp.startswith("_") and not dp.endswith("_")
            ),
            key=lambda d: -diphone_freq.get(d, 0),
        )
        vowel_set = set(vowels)
        # Use a full, clear stressed carrier vowel (e.g. 'a') + unstressed '@'
        # so VCCV supplements sound like natural two-syllable logatomes ("alsha", "afba")
        # and render cleanly through eSpeak without reducing the first vowel.
        lead_v = "a" if "a" in vowel_set else (vowels[0] if vowels else "a")
        tail_v = "@" if "@" in vowel_set else lead_v
        supplements: List[Dict[str, object]] = []
        for dp in uncovered:
            p1, p2 = dp.split("_", 1)
            if p1 not in vowel_set and p2 not in vowel_set:
                phones = [lead_v, p1, p2, tail_v]
                kind = "supplement_vccv"
            else:
                phones = [p1, p2]
                kind = "supplement_pair"
            supplements.append({
                "kind": kind,
                "display": " ".join(phones),
                "phones": phones,
                "targets": [dp],
            })
        return supplements

    is_en = voice.lower().startswith("en")
    illegal_onset = {"N"} if is_en else set()
    illegal_coda = {"h", "w", "j", "r"} if (is_en and "gb" in voice.lower()) else ({"h", "w", "j"} if is_en else set())

    missing_cv: List[Tuple[str, str]] = []
    missing_vc: List[Tuple[str, str]] = []
    for c in consonants:
        if c not in illegal_onset:
            for v in vowels:
                if f"{c}_{v}" not in covered_diphones:
                    missing_cv.append((c, v))
    for v in vowels:
        for c in consonants:
            if c not in illegal_coda:
                if f"{v}_{c}" not in covered_diphones:
                    missing_vc.append((v, c))

    by_vowel_cv: Dict[str, List[str]] = collections.defaultdict(list)
    by_vowel_vc: Dict[str, List[str]] = collections.defaultdict(list)
    for c, v in missing_cv:
        by_vowel_cv[v].append(c)
    for v, c in missing_vc:
        by_vowel_vc[v].append(c)

    supplements = []
    for v in vowels:
        cvs = by_vowel_cv.get(v, [])
        vcs = by_vowel_vc.get(v, [])
        paired = min(len(cvs), len(vcs))
        for k in range(paired):
            c1, c2 = cvs[k], vcs[k]
            supplements.append({
                "kind": "supplement_cvc",
                "display": f"{c1} {v} {c2}",
                "phones": [c1, v, c2],
                "targets": [f"{c1}_{v}", f"{v}_{c2}"],
            })
        for c1 in cvs[paired:]:
            supplements.append({
                "kind": "supplement_cv",
                "display": f"{c1} {v}",
                "phones": [c1, v],
                "targets": [f"{c1}_{v}"],
            })
        for c2 in vcs[paired:]:
            supplements.append({
                "kind": "supplement_vc",
                "display": f"{v} {c2}",
                "phones": [v, c2],
                "targets": [f"{v}_{c2}"],
            })
    return supplements


def main() -> None:
    args = parse_args()
    is_english = args.voice.lower().startswith("en")
    raw_words = load_wordlist(args.wordlist, args.max_vocab, ascii_only=is_english)
    print(f"Loaded {len(raw_words)} clean words from {args.wordlist}")

    x_lines, ipa_lines = batch_phonemize(
        [w for w, _ in raw_words],
        voice=args.voice,
        espeak_bin=args.espeak_bin,
        espeak_data=args.espeak_data,
    )

    vowel_set_preset, cons_set_preset, norm_map = get_normalization(args.voice, args.bath)
    canonical_preset = (vowel_set_preset | cons_set_preset) if (vowel_set_preset and cons_set_preset) else None

    entries: List[WordEntry] = []
    phone_counts: collections.Counter = collections.Counter()
    phone_ipa: Dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    diphone_word_count: collections.Counter = collections.Counter()
    diphone_freq: collections.Counter = collections.Counter()
    diphone_examples: Dict[str, List[str]] = collections.defaultdict(list)

    for rank, ((w, freq), xl, il) in enumerate(zip(raw_words, x_lines, ipa_lines), start=1):
        parsed = normalize_word_phones(xl, il, norm_map, canonical_preset)
        if parsed is None:
            continue
        phones, stresses, ipas = parsed
        dips = extract_diphones_with_quality(phones, stresses)
        entries.append(WordEntry(
            word=w,
            freq=freq,
            rank=rank,
            phones=phones,
            stresses=stresses,
            ipa_phones=ipas,
            raw_espeak=xl.strip(),
            diphones=dips,
        ))
        for ph, ip in zip(phones, ipas):
            phone_counts[ph] += 1
            if ip:
                phone_ipa[ph][ip] += 1
        for dp in dips:
            diphone_word_count[dp] += 1
            diphone_freq[dp] += freq
            if len(diphone_examples[dp]) < 5:
                diphone_examples[dp].append(w)

    # Determine vowels/consonants (preset or heuristic from IPA for arbitrary languages)
    if vowel_set_preset is not None and cons_set_preset is not None:
        vowels = [v for v in EN_GB_VOWELS if v in phone_counts]
        consonants = [c for c in EN_GB_CONSONANTS if c in phone_counts]
    else:
        # Filter rare foreign-loanword artifact phones (< 0.1% of scanned words)
        min_phone_words = max(10, int(0.001 * len(entries)))
        native_phones = {ph for ph, cnt in phone_counts.items() if cnt >= min_phone_words}
        if len(native_phones) >= 12 and len(native_phones) < len(phone_counts):
            dropped_phs = sorted(set(phone_counts) - native_phones)
            print(f"Filtered {len(dropped_phs)} rare loanword phone(s) (<{min_phone_words} words): {dropped_phs}")
            entries = [e for e in entries if all(p in native_phones for p in e.phones)]
            phone_counts = collections.Counter()
            diphone_word_count = collections.Counter()
            diphone_freq = collections.Counter()
            diphone_examples = collections.defaultdict(list)
            for e in entries:
                for ph in set(e.phones):
                    phone_counts[ph] += 1
                for dp in e.diphones:
                    diphone_word_count[dp] += 1
                    diphone_freq[dp] += e.freq
                    if len(diphone_examples[dp]) < 5:
                        diphone_examples[dp].append(e.word)

        ipa_vowel_chars = set("aeiouyɑɒæɐəɜɛɪɔøœʊʌʏʉɯɨɵɤ")
        vowels, consonants = [], []
        for ph, _ in phone_counts.most_common():
            top_ip = phone_ipa[ph].most_common(1)[0][0] if phone_ipa[ph] else ""
            if any(ch in ipa_vowel_chars for ch in top_ip):
                vowels.append(ph)
            else:
                consonants.append(ph)
    vowel_set = set(vowels)

    # Real-text target diphones: word-internal + word-edge occurring in >= min_diphone_words
    all_real_diphones = set(diphone_word_count.keys())
    target_diphones = {
        dp for dp, cnt in diphone_word_count.items()
        if cnt >= args.min_diphone_words
    }
    # Always include word edges for every active phone even if rare
    for ph in vowels + consonants:
        for edge in (f"_{ph}", f"{ph}_"):
            if edge in all_real_diphones:
                target_diphones.add(edge)

    # Resolve automatic ambiguity filters
    vlow = args.voice.lower()
    if args.skip_symbols == "auto":
        skip_syms = {"aa"} if vlow.startswith("en-gb") else set()
    elif args.skip_symbols.lower() == "none" or not args.skip_symbols.strip():
        skip_syms = set()
    else:
        skip_syms = {s.strip() for s in args.skip_symbols.split(",") if s.strip()}

    if args.compare_voice == "auto":
        if vlow == "en-gb":
            compare_voice = "en-gb-x-rp"
        elif vlow == "en-gb-x-rp":
            compare_voice = "en-gb"
        else:
            compare_voice = None
    elif args.compare_voice.lower() == "none" or not args.compare_voice.strip():
        compare_voice = None
    else:
        compare_voice = args.compare_voice

    disagreeing_words: Set[str] = set()
    if compare_voice:
        cmp_x_lines, cmp_ipa_lines = batch_phonemize(
            [w for w, _ in raw_words[:args.candidate_vocab]],
            voice=compare_voice,
            espeak_bin=args.espeak_bin,
            espeak_data=args.espeak_data,
        )
        _, _, cmp_norm = get_normalization(compare_voice, args.bath)
        for (w, _), xl1, il1, xl2, il2 in zip(
            raw_words[:args.candidate_vocab], x_lines, ipa_lines, cmp_x_lines, cmp_ipa_lines
        ):
            p1 = normalize_word_phones(xl1, il1, norm_map, canonical_preset)
            p2 = normalize_word_phones(xl2, il2, cmp_norm, canonical_preset)
            if p1 is None or p2 is None or p1[0] != p2[0]:
                disagreeing_words.add(w)
        print(f"Dialect check ({args.voice} vs {compare_voice}): excluded {len(disagreeing_words)} disagreeing candidate words")

    def has_skipped_symbol(entry: WordEntry) -> bool:
        if not skip_syms:
            return False
        raw_toks = {t.lstrip("',") for t in entry.raw_espeak.split("_") if t}
        return bool(raw_toks & skip_syms)

    candidates = [
        e for e in entries
        if is_good_prompt_candidate(
            e, args.min_len, args.max_len, args.candidate_vocab, is_english, vowel_set
        )
        and not has_skipped_symbol(e)
        and e.word not in disagreeing_words
    ]

    print(f"Parsed words: {len(entries)} | Prompt candidates: {len(candidates)}")
    print(f"Active phones: {len(vowels)} vowels + {len(consonants)} consonants = {len(vowels) + len(consonants)}")
    print(f"Real-text diphones (all in vocab): {len(all_real_diphones)}")
    print(f"Real-text target diphones (>= {args.min_diphone_words} words): {len(target_diphones)}")

    initial_counts: collections.Counter[str] = collections.Counter()
    exclude_words: Set[str] = set()
    if args.extend_from is not None:
        ext_dmap = json.loads((args.extend_from / "diphone_map.json").read_text(encoding="utf-8"))
        for item in ext_dmap.get("lines", []):
            if item.get("kind") == "word":
                exclude_words.add(str(item["text"]).lower())
            phs = list(item.get("phones", []))
            for a, b in zip(phs, phs[1:]):
                initial_counts[f"{a}_{b}"] += 1
            if phs:
                initial_counts[f"_{phs[0]}"] += 1
                initial_counts[f"{phs[-1]}_"] += 1
        print(f"Extending from {args.extend_from}: {len(exclude_words)} existing words, {len(initial_counts)} diphones already present")

    selected, curve = greedy_set_cover(
        candidates=candidates,
        target_diphones=target_diphones,
        diphone_freq=diphone_freq,
        max_words=args.max_words,
        target_coverage=args.target_word_coverage,
        min_takes=args.min_takes,
        initial_counts=initial_counts,
        exclude_words=exclude_words,
    )

    covered_by_words: Set[str] = set(initial_counts.keys())
    for e in selected:
        covered_by_words.update(e.diphones.keys())

    # Compute core CV / VC coverage before supplement
    is_gb = is_english and "gb" in args.voice.lower()
    illegal_onset = {"N"} if is_english else set()
    illegal_coda = {"h", "w", "j", "r"} if is_gb else ({"h", "w", "j"} if is_english else set())
    legal_cv = {f"{c}_{v}" for c in consonants if c not in illegal_onset for v in vowels}
    legal_vc = {f"{v}_{c}" for v in vowels for c in consonants if c not in illegal_coda}
    cv_hit = len(legal_cv & covered_by_words)
    vc_hit = len(legal_vc & covered_by_words)
    cc_hit = sum(1 for dp in covered_by_words if "_" in dp and not dp.startswith("_") and not dp.endswith("_")
                 and dp.split("_")[0] not in vowel_set and dp.split("_")[1] not in vowel_set)
    vv_hit = sum(1 for dp in covered_by_words if "_" in dp and not dp.startswith("_") and not dp.endswith("_")
                 and dp.split("_")[0] in vowel_set and dp.split("_")[1] in vowel_set)

    supplements = build_supplement(
        covered_by_words,
        target_diphones,
        diphone_freq,
        vowels,
        consonants,
        args.voice,
        mode=args.supplement_mode,
    )

    # Write outputs
    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    # 1. inventory.json
    inv = {
        "voice": args.voice,
        "bath_mode": args.bath,
        "vowels": vowels,
        "consonants": consonants,
        "ipa_map": {
            ph: (phone_ipa[ph].most_common(1)[0][0] if phone_ipa[ph] else EN_GB_IPA_DEFAULTS.get(ph, ph))
            for ph in vowels + consonants
        },
        "normalization": {k: list(v) for k, v in sorted(norm_map.items())},
        "phone_word_counts": {ph: phone_counts[ph] for ph in vowels + consonants},
    }
    (out_dir / "inventory.json").write_text(json.dumps(inv, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    # 2. coverage_curve.csv
    with (out_dir / "coverage_curve.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["step", "word", "rank", "phones", "new_diphones", "cum_diphones", "type_coverage", "token_coverage", "added"],
        )
        writer.writeheader()
        writer.writerows(curve)

    # 3. recording_script.txt & diphone_map.json
    script_lines: List[str] = []
    diphone_lines: List[Dict[str, object]] = []
    line_no = 0
    for e in selected:
        line_no += 1
        script_lines.append(f"{line_no:04d}  {e.word}")
        diphone_lines.append({
            "line": line_no,
            "kind": "word",
            "unit_type": "word",
            "text": e.word,
            "phones": list(e.phones),
            "ipa": list(e.ipa_phones),
            "stresses": list(e.stresses),
            "raw_espeak": e.raw_espeak,
            "diphones": sorted(e.diphones.keys()),
            "vocab_rank": e.rank,
        })
    for sup in supplements:
        line_no += 1
        script_lines.append(f"{line_no:04d}  [{sup['display']}]")
        diphone_lines.append({
            "line": line_no,
            "kind": sup["kind"],
            "unit_type": sup["kind"],
            "text": sup["display"],
            "phones": sup["phones"],
            "ipa": [inv["ipa_map"].get(p, p) for p in sup["phones"]],
            "diphones": sup["targets"],
        })

    diphone_map_doc = {
        "voice": args.voice,
        "vowels": vowels,
        "consonants": consonants,
        "word_count": len(selected),
        "supplement_count": len(supplements),
        "lines": diphone_lines,
    }
    (out_dir / "recording_script.txt").write_text("\n".join(script_lines) + "\n", encoding="utf-8")
    (out_dir / "diphone_map.json").write_text(json.dumps(diphone_map_doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    # 4. uncovered_diphones.csv
    uncovered_real = sorted(target_diphones - covered_by_words, key=lambda d: -diphone_freq[d])
    with (out_dir / "uncovered_diphones.csv").open("w", encoding="utf-8", newline="") as f:
        w_csv = csv.writer(f)
        w_csv.writerow(["diphone", "word_count", "freq_sum", "examples"])
        for dp in uncovered_real:
            w_csv.writerow([dp, diphone_word_count[dp], diphone_freq[dp], " ".join(diphone_examples[dp])])

    # 5. plan_report.md + console summary
    milestones = [50, 100, 150, 200, 250, 300, 350, 400, 450, 500, len(selected)]
    seen_m = set()
    milestone_rows = []
    for m in milestones:
        if m <= len(curve) and m not in seen_m:
            seen_m.add(m)
            c = curve[m - 1]
            milestone_rows.append(
                f"| {m:4d} | {c['cum_diphones']:4d} / {len(target_diphones)} | "
                f"{100.0 * float(c['type_coverage']):5.1f}% | "
                f"{100.0 * float(c['token_coverage']):6.2f}% |"
            )

    total_real_covered = len(all_real_diphones & covered_by_words)
    total_real_freq_all = sum(diphone_freq.values())
    covered_real_freq_all = sum(diphone_freq[dp] for dp in (all_real_diphones & covered_by_words))

    report = [
        f"# Hybrid Word Corpus Plan — `{args.voice}`",
        "",
        "## Summary",
        f"- **Canonical inventory:** {len(vowels)} vowels + {len(consonants)} consonants = **{len(vowels) + len(consonants)} phones**",
        f"- **Vocabulary scanned:** top {len(entries):,} words (`{args.wordlist.name}`)",
        f"- **Real words selected:** **{len(selected)}**",
        f"- **Minimal-pair supplement tail (for remaining legal CV/VC):** **{len(supplements)}**",
        f"- **Total recording prompts:** **{len(selected) + len(supplements)}** (vs. 1,666 in the old US English nonsense script)",
        "",
        "## Coverage Achieved by Real Words Alone",
        f"- **Target real-text diphones (>= {args.min_diphone_words} words):** {len(target_diphones & covered_by_words)} / {len(target_diphones)} ({100.0 * len(target_diphones & covered_by_words) / max(1, len(target_diphones)):.1f}%)",
        f"- **Running-speech weighted transition coverage (all top-20k words):** **{100.0 * covered_real_freq_all / max(1, total_real_freq_all):.2f}%** ({total_real_covered} / {len(all_real_diphones)} unique pairs)",
        f"- **Legal CV covered by words:** {cv_hit} / {len(legal_cv)} ({100.0 * cv_hit / max(1, len(legal_cv)):.1f}%)",
        f"- **Legal VC covered by words:** {vc_hit} / {len(legal_vc)} ({100.0 * vc_hit / max(1, len(legal_vc)):.1f}%)",
        f"- **Bonus CC clusters covered naturally by words:** {cc_hit}",
        f"- **Bonus VV transitions covered naturally by words:** {vv_hit}",
        "",
        "## Coverage Curve",
        "| Words | Target Diphones | Unique Target % | Running-Speech % |",
        "| --- | --- | --- | --- |",
        *milestone_rows,
        "",
        "## First 60 Selected Words",
        ", ".join(e.word for e in selected[:60]),
        "",
        "## Last 40 Selected Words (tail)",
        ", ".join(e.word for e in selected[-40:]),
        "",
        f"## Minimal-Pair Supplement Tail ({len(supplements)} prompts, first 30 shown)",
        ", ".join(f"`{s['display']}`" for s in supplements[:30]),
    ]
    report_text = "\n".join(report) + "\n"
    (out_dir / "plan_report.md").write_text(report_text, encoding="utf-8")
    print()
    print(report_text)


if __name__ == "__main__":
    main()
