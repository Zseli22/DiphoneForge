"""Automatic eSpeak-IPA -> MFA acoustic model phone mapper with strict inventory verification.

Every MFA model uses a slightly different phone set (`english_mfa` uses `ɐ`,
`ej`, `əw`, `oː`; Vox Communis `*_cv` models use XPF/Epitran conventions).
This module maps any language's discovered canonical eSpeak IPA inventory onto
the target MFA model's exact phone set, asserts 100% membership, and reports
any approximate fallbacks transparently.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Set, Tuple

# Verified english_mfa v3.x phone map (matching tools/align_word_corpus.py)
ENGLISH_MFA_OVERRIDES: Dict[str, List[str]] = {
    "i:": ["iː"], "I": ["ɪ"], "E": ["ɛ"], "a": ["a"], "A:": ["ɑː"],
    "0": ["ɒ"], "O:": ["oː"], "V": ["ɐ"], "U": ["ʊ"], "u:": ["uː"],
    "@": ["ə"], "3:": ["ɜː"],
    "eI": ["ej"], "aI": ["aj"], "OI": ["ɔj"], "aU": ["aw"], "oU": ["əw"],
    "i@": ["ɪ", "ə"], "e@": ["ɛː"], "U@": ["ʊ", "ə"],
    "p": ["p"], "t": ["t"], "k": ["k"], "b": ["b"], "d": ["d"], "g": ["ɡ"],
    "tS": ["tʃ"], "dZ": ["dʒ"],
    "f": ["f"], "v": ["v"], "T": ["θ"], "D": ["ð"],
    "s": ["s"], "z": ["z"], "S": ["ʃ"], "Z": ["ʒ"], "h": ["h"],
    "m": ["m"], "n": ["n"], "N": ["ŋ"],
    "l": ["l"], "L": ["ɫ"], "r": ["ɹ"], "w": ["w"], "j": ["j"],
}

ENGLISH_MFA_VARIANTS: Dict[str, str] = {
    "pʰ": "p", "tʰ": "t", "kʰ": "k", "ɫ": "l", "a": "æ", "oː": "ɔ", "əw": "ow",
}

# Universal articulatory IPA features for nearest-neighbor fallback
IPA_VOWEL_FEATURES: Dict[str, Tuple[float, float, float]] = {
    "i": (1.0, 1.0, 0.0), "y": (1.0, 1.0, 1.0), "ɪ": (1.4, 1.2, 0.0), "ʏ": (1.4, 1.2, 1.0),
    "e": (2.0, 1.0, 0.0), "ø": (2.0, 1.0, 1.0), "ɛ": (3.0, 1.0, 0.0), "œ": (3.0, 1.0, 1.0),
    "æ": (3.6, 1.0, 0.0), "a": (4.0, 1.4, 0.0), "ɶ": (4.0, 1.2, 1.0),
    "ɨ": (1.0, 2.0, 0.0), "ʉ": (1.0, 2.0, 1.0), "ə": (2.5, 2.0, 0.0), "ɘ": (2.0, 2.0, 0.0),
    "ɵ": (2.0, 2.0, 1.0), "ɜ": (3.0, 2.0, 0.0), "ɞ": (3.0, 2.0, 1.0), "ɐ": (3.5, 2.0, 0.0),
    "ɯ": (1.0, 3.0, 0.0), "u": (1.0, 3.0, 1.0), "ʊ": (1.5, 2.7, 1.0),
    "ɤ": (2.0, 3.0, 0.0), "o": (2.0, 3.0, 1.0), "ʌ": (3.0, 2.7, 0.0), "ɔ": (3.0, 3.0, 1.0),
    "ɑ": (4.0, 3.0, 0.0), "ɒ": (4.0, 3.0, 1.0),
}

# (place 1..8, manner 1=stop..6=glide, voiced 0/1)
IPA_CONS_FEATURES: Dict[str, Tuple[float, float, float]] = {
    "p": (1.0, 1.0, 0.0), "b": (1.0, 1.0, 1.0), "m": (1.0, 4.0, 1.0),
    "β": (1.0, 3.0, 1.0), "ɸ": (1.0, 3.0, 0.0),
    "f": (2.0, 3.0, 0.0), "v": (2.0, 3.0, 1.0), "ʋ": (2.0, 6.0, 1.0), "ɱ": (2.0, 4.0, 1.0),
    "θ": (3.0, 3.0, 0.0), "ð": (3.0, 3.0, 1.0),
    "t": (4.0, 1.0, 0.0), "d": (4.0, 1.0, 1.0), "s": (4.0, 3.0, 0.0), "z": (4.0, 3.0, 1.0),
    "n": (4.0, 4.0, 1.0), "l": (4.0, 5.0, 1.0), "ɫ": (4.0, 5.0, 1.0), "ɬ": (4.0, 3.0, 0.0),
    "r": (4.0, 5.0, 1.0), "ɹ": (4.0, 5.0, 1.0), "ɾ": (4.0, 5.0, 1.0), "ɽ": (4.5, 5.0, 1.0),
    "ts": (4.0, 2.0, 0.0), "dz": (4.0, 2.0, 1.0),
    "ʃ": (5.0, 3.0, 0.0), "ʒ": (5.0, 3.0, 1.0), "ʂ": (5.0, 3.0, 0.0), "ʐ": (5.0, 3.0, 1.0),
    "ɕ": (5.3, 3.0, 0.0), "ʑ": (5.3, 3.0, 1.0),
    "tʃ": (5.0, 2.0, 0.0), "dʒ": (5.0, 2.0, 1.0), "tɕ": (5.3, 2.0, 0.0), "dʑ": (5.3, 2.0, 1.0),
    "ʈ": (5.0, 1.0, 0.0), "ɖ": (5.0, 1.0, 1.0), "ɳ": (5.0, 4.0, 1.0), "ɭ": (5.0, 5.0, 1.0),
    "c": (6.0, 1.0, 0.0), "ɟ": (6.0, 1.0, 1.0), "ç": (6.0, 3.0, 0.0), "ʝ": (6.0, 3.0, 1.0),
    "ɲ": (6.0, 4.0, 1.0), "ʎ": (6.0, 5.0, 1.0), "j": (6.0, 6.0, 1.0), "ɥ": (6.0, 6.0, 1.0),
    "k": (7.0, 1.0, 0.0), "ɡ": (7.0, 1.0, 1.0), "g": (7.0, 1.0, 1.0),
    "x": (7.0, 3.0, 0.0), "ɣ": (7.0, 3.0, 1.0), "ŋ": (7.0, 4.0, 1.0),
    "q": (7.5, 1.0, 0.0), "ɢ": (7.5, 1.0, 1.0), "χ": (7.5, 3.0, 0.0), "ʁ": (7.5, 3.0, 1.0),
    "ħ": (7.8, 3.0, 0.0), "ʕ": (7.8, 3.0, 1.0),
    "h": (8.0, 3.0, 0.0), "ɦ": (8.0, 3.0, 1.0), "ʔ": (8.0, 1.0, 0.0),
    "w": (1.5, 6.0, 1.0),
}

# Common IPA equivalents / transcription variants across MFA & Vox Communis models
IPA_EQUIVALENTS: Dict[str, List[str]] = {
    "ɡ": ["g", "ɡ"],
    "g": ["ɡ", "g"],
    "ɹ": ["ɹ", "r", "ɾ", "ɻ"],
    "r": ["r", "ɾ", "ɹ", "ʁ"],
    "ɾ": ["ɾ", "r", "ɹ"],
    "tʃ": ["tʃ", "t͡ʃ", "ʧ", "c"],
    "dʒ": ["dʒ", "d͡ʒ", "ʤ", "ɟ"],
    "ts": ["ts", "t͡s", "ʦ"],
    "dz": ["dz", "d͡z", "ʣ"],
    "tɕ": ["tɕ", "t͡ɕ", "tʃ", "t͡ʃ"],
    "dʑ": ["dʑ", "d͡ʑ", "dʒ", "d͡ʒ"],
    "a": ["a", "æ", "ɑ", "ɐ"],
    "ɛ": ["ɛ", "e"],
    "e": ["e", "ɛ"],
    "ɔ": ["ɔ", "o", "ɒ"],
    "o": ["o", "ɔ"],
    "ɒ": ["ɒ", "ɔ", "ɑ", "o"],
    "ʌ": ["ɐ", "ʌ", "ə", "a"],
    "ɜ": ["ɜ", "ə", "ɛ"],
    "ɪ": ["ɪ", "i", "e"],
    "ʊ": ["ʊ", "u", "o"],
    "ɲ": ["ɲ", "nʲ", "n"],
    "ʎ": ["ʎ", "lʲ", "l"],
    "c": ["c", "tʲ", "kʲ", "tʃ"],
    "ɟ": ["ɟ", "dʲ", "ɡʲ", "dʒ"],
}


@dataclass
class PhoneMapResult:
    mapping: Dict[str, List[str]]
    exact: List[str]
    approximate: List[Tuple[str, str, List[str]]]  # (espeak_ph, ipa, chosen_mfa_phones)


def _clean_ipa(s: str) -> str:
    return (
        s.strip()
        .replace("ˈ", "")
        .replace("ˌ", "")
        .replace(":", "ː")
        .replace("g", "ɡ")
        .replace("͡", "")
        .replace("͜", "")
    )


def _strip_diacritics(s: str) -> str:
    nfd = unicodedata.normalize("NFD", s)
    out = "".join(ch for ch in nfd if unicodedata.category(ch) not in ("Mn", "Me") and ch not in ("ː", "ˑ", "ʰ", "ʲ", "ʷ"))
    return unicodedata.normalize("NFC", out)


def _nearest_model_phone(ipa: str, is_vowel: bool, model_phones: Sequence[str]) -> str:
    cleaned = _strip_diacritics(_clean_ipa(ipa))
    table = IPA_VOWEL_FEATURES if is_vowel else IPA_CONS_FEATURES
    target = table.get(cleaned) or (table.get(cleaned[0]) if cleaned else None)

    best_phone = None
    best_dist = 1e9
    for mp in model_phones:
        if mp in ("sil", "sp", "spn", "<eps>"):
            continue
        mp_clean = _strip_diacritics(_clean_ipa(mp))
        cand = table.get(mp_clean) or (table.get(mp_clean[0]) if mp_clean else None)
        if cand is None or target is None:
            continue
        # Weight manner & voicing heavily for consonants; height & backness for vowels
        if is_vowel:
            d = (target[0] - cand[0]) ** 2 + (target[1] - cand[1]) ** 2 + 0.8 * (target[2] - cand[2]) ** 2
            # Prefer matching length if possible
            if ("ː" in ipa) != ("ː" in mp):
                d += 0.25
        else:
            d = (target[0] - cand[0]) ** 2 + 3.0 * (target[1] - cand[1]) ** 2 + 4.0 * (target[2] - cand[2]) ** 2
        if d < best_dist:
            best_dist = d
            best_phone = mp

    if best_phone is not None:
        return best_phone
    # Last resort: first non-special model phone
    for mp in model_phones:
        if mp not in ("sil", "sp", "spn", "<eps>"):
            return mp
    raise RuntimeError("MFA model phone inventory is empty.")


def build_phone_map(
    inventory: Dict[str, object],
    model_name: str,
    model_phones: Sequence[str],
    custom_overrides: Optional[Dict[str, List[str]]] = None,
) -> PhoneMapResult:
    """Map every canonical phone in `inventory` onto verified tokens in `model_phones`."""
    model_set: Set[str] = set(model_phones)
    vowels = set(inventory["vowels"])
    consonants = list(inventory["consonants"])
    all_phones = list(inventory["vowels"]) + consonants
    ipa_map: Dict[str, str] = dict(inventory.get("ipa_map", {}))

    overrides: Dict[str, List[str]] = {}
    if model_name == "english_mfa":
        overrides.update(ENGLISH_MFA_OVERRIDES)
    if custom_overrides:
        for k, v in custom_overrides.items():
            overrides[k] = list(v) if isinstance(v, list) else [str(v)]

    mapping: Dict[str, List[str]] = {}
    exact: List[str] = []
    approximate: List[Tuple[str, str, List[str]]] = []

    for ph in all_phones:
        ipa = str(ipa_map.get(ph, ph))
        is_v = ph in vowels

        # 1. Verified override (only if every target symbol is actually in this model!)
        if ph in overrides and all(t in model_set for t in overrides[ph]):
            mapping[ph] = list(overrides[ph])
            exact.append(ph)
            continue

        # 2. Direct IPA match (raw, cleaned, tie-bar, length-stripped, or g/ɡ alias)
        cands = [
            ipa,
            _clean_ipa(ipa),
            ipa.replace("ː", ""),
            _clean_ipa(ipa).replace("ː", ""),
            ipa.replace("ɡ", "g"),
            ipa.replace("g", "ɡ"),
            ipa.replace(":", "ː"),
            ipa.replace("ː", ":"),
        ]
        if len(_clean_ipa(ipa)) == 2:
            c2 = _clean_ipa(ipa)
            cands.append(c2[0] + "\u0361" + c2[1])
        hit = next((c for c in cands if c in model_set), None)
        if hit is not None:
            mapping[ph] = [hit]
            exact.append(ph)
            continue

        # 3. Known IPA transcription equivalents
        base_clean = _clean_ipa(ipa).replace("ː", "")
        eq_hit = None
        for eq in IPA_EQUIVALENTS.get(base_clean, []):
            if eq in model_set:
                eq_hit = eq
                break
            if ("ː" in ipa) and (eq + "ː") in model_set:
                eq_hit = eq + "ː"
                break
        if eq_hit is not None:
            mapping[ph] = [eq_hit]
            approximate.append((ph, ipa, [eq_hit]))
            continue

        # 4. Multi-character diphthong / affricate split into 2 valid model phones
        stripped = _clean_ipa(ipa).replace("ː", "")
        if len(stripped) == 2:
            p1, p2 = stripped[0], stripped[1]
            m1 = next((c for c in [p1, *IPA_EQUIVALENTS.get(p1, [])] if c in model_set), None)
            m2 = next((c for c in [p2, *IPA_EQUIVALENTS.get(p2, [])] if c in model_set), None)
            if m1 and m2:
                mapping[ph] = [m1, m2]
                approximate.append((ph, ipa, [m1, m2]))
                continue

        # 5. Universal articulatory nearest neighbor
        nn = _nearest_model_phone(ipa, is_v, model_phones)
        mapping[ph] = [nn]
        approximate.append((ph, ipa, [nn]))

    # Final hard assertion: zero unverified symbols can ever enter mfa_dict.txt
    for ph, seq in mapping.items():
        for sym in seq:
            if sym not in model_set:
                raise AssertionError(f"Internal error: mapped phone {ph!r} -> {sym!r} not in {model_name}")

    return PhoneMapResult(mapping=mapping, exact=exact, approximate=approximate)
