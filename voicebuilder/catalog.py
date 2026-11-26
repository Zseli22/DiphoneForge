"""Language catalog loader mapping eSpeak NG voices, FrequencyWords lists, and MFA models."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from .paths import get_app_root


@dataclass(frozen=True)
class LanguageSpec:
    id: str
    name: str
    espeak_voice: str
    espeak_phonemes: str
    espeak_lang_lines: List[str]
    wordlist_code: Optional[str]
    mfa_model: str
    mfa_tier: str  # "mfa" or "cv"
    dict_code: Optional[str] = None
    ascii_only: bool = False
    compare_voice: Optional[str] = None
    skip_symbols: List[str] = field(default_factory=list)
    notes: str = ""
    preview_text: str = ""

    @property
    def has_auto_wordlist(self) -> bool:
        return bool(self.wordlist_code)

    @property
    def has_dictionary(self) -> bool:
        return bool(self.dict_code)

    @property
    def display_label(self) -> str:
        tier = "MFA IPA" if self.mfa_tier == "mfa" else "MFA CommonVoice"
        wl = "auto word list" if self.has_auto_wordlist else "custom word list"
        return f"{self.name} [{self.id}] — {tier}, {wl}"


class LanguageCatalog:
    def __init__(self, catalog_path: Optional[Path] = None) -> None:
        self.path = catalog_path or (get_app_root() / "data" / "language_catalog.json")
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.wordlist_base_url: str = raw["wordlist_source"]["base_url"].rstrip("/")
        self.dictionary_base_url: str = (
            raw.get("dictionary_source", {}).get("base_url")
            or "https://raw.githubusercontent.com/wooorm/dictionaries/main/dictionaries"
        ).rstrip("/")
        self._specs: Dict[str, LanguageSpec] = {}
        self._ordered: List[LanguageSpec] = []
        for item in raw["languages"]:
            spec = LanguageSpec(
                id=item["id"],
                name=item["name"],
                espeak_voice=item["espeak_voice"],
                espeak_phonemes=item["espeak_phonemes"],
                espeak_lang_lines=list(item.get("espeak_lang_lines") or [f"language {item['espeak_voice']} 5"]),
                wordlist_code=item.get("wordlist_code"),
                dict_code=item.get("dict_code"),
                mfa_model=item["mfa_model"],
                mfa_tier=item.get("mfa_tier", "mfa"),
                ascii_only=bool(item.get("ascii_only", False)),
                compare_voice=item.get("compare_voice"),
                skip_symbols=list(item.get("skip_symbols") or []),
                notes=str(item.get("notes", "")),
                preview_text=str(item.get("preview_text", "")),
            )
            self._specs[spec.id.lower()] = spec
            self._ordered.append(spec)

    def all(self) -> List[LanguageSpec]:
        return list(self._ordered)

    def get(self, lang_id: str) -> LanguageSpec:
        key = lang_id.strip().lower()
        if key not in self._specs:
            avail = ", ".join(s.id for s in self._ordered)
            raise KeyError(f"Unknown language {lang_id!r}. Available: {avail}")
        return self._specs[key]

    def wordlist_urls(self, wordlist_code: str) -> List[str]:
        """Return preferred FrequencyWords URLs (50k first, fallback to full)."""
        code = wordlist_code.strip()
        return [
            f"{self.wordlist_base_url}/{code}/{code}_50k.txt",
            f"{self.wordlist_base_url}/{code}/{code}_full.txt",
        ]

    def dictionary_url_pairs(self, dict_code: str) -> List[tuple[str, str]]:
        """Return candidate `(dic_url, aff_url)` pairs in wooorm/dictionaries."""
        code = dict_code.strip()
        variants: List[str] = [code]
        if code.lower() not in variants:
            variants.append(code.lower())
        if "-" in code:
            base = code.split("-", 1)[0].lower()
            if base not in variants:
                variants.append(base)
        return [
            (
                f"{self.dictionary_base_url}/{v}/index.dic",
                f"{self.dictionary_base_url}/{v}/index.aff",
            )
            for v in variants
        ]
