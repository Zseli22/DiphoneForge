"""Pure-Python Hunspell `.dic` / `.aff` dictionary validator (zero third-party dependencies).

Validates candidate prompt words for language authenticity and distinguishes:
  - `"lower"`: normal lowercase dictionary words (including inflections & compounds)
  - `"cap"`:   proper nouns / capitalized-only dictionary entries (e.g. Saturday, November)
  - `"unknown"`: typos (OCR `couid`/`iong`), truncated contractions (`shouldn`),
                 interjections/slang, and untranslated foreign words.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Pattern, Set, Tuple


@dataclass(frozen=True)
class _AffixRule:
    flag: str
    cross: bool
    strip: str
    add: str
    cond_re: Optional[Pattern[str]]
    cont_flags: Tuple[str, ...]


def _compile_cond(cond: str, *, is_suffix: bool) -> Optional[Pattern[str]]:
    if not cond or cond == ".":
        return None
    pat = f"(?:{cond})$" if is_suffix else f"^(?:{cond})"
    try:
        return re.compile(pat)
    except re.error:
        return None


def _parse_flags(raw: str, flag_mode: str) -> Tuple[str, ...]:
    if not raw:
        return ()
    if flag_mode == "num":
        return tuple(p for p in raw.split(",") if p)
    if flag_mode == "long":
        return tuple(raw[i : i + 2] for i in range(0, len(raw) - 1, 2))
    return tuple(raw)


def _decode_bytes(data: bytes, encoding: str) -> str:
    for enc in (encoding, "utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except Exception:
            continue
    return data.decode("utf-8", errors="replace")


class HunspellValidator:
    """Fast reverse-lookup Hunspell validator for filtering prompt word lists."""

    def __init__(self, dic_path: Path, aff_path: Optional[Path] = None) -> None:
        self.dic_path = dic_path
        self.aff_path = aff_path if (aff_path and aff_path.is_file()) else None

        self.encoding = "utf-8"
        self.flag_mode = "default"
        self.forbidden_flag: Optional[str] = None
        self.needaffix_flag: Optional[str] = None
        self.onlyincompound_flag: Optional[str] = None
        self.compound_flag: Optional[str] = None
        self.compound_begin_flag: Optional[str] = None
        self.compound_end_flag: Optional[str] = None
        self.has_compounding = False

        self.sfx_by_add: Dict[str, List[_AffixRule]] = {}
        self.pfx_by_add: Dict[str, List[_AffixRule]] = {}
        self.max_sfx_len = 0
        self.max_pfx_len = 0

        self.lower_stems: Dict[str, Set[str]] = {}
        self.cap_stems: Dict[str, Set[str]] = {}
        self.compound_only_stems: Set[str] = set()
        self._cache: Dict[str, str] = {}

        if self.aff_path is not None:
            self._load_aff(self.aff_path)
        self._load_dic(self.dic_path)

    def _load_aff(self, path: Path) -> None:
        raw_bytes = path.read_bytes()
        # Peek encoding from SET header
        head = raw_bytes[:1024].decode("ascii", errors="ignore")
        for ln in head.splitlines():
            parts = ln.strip().split()
            if len(parts) >= 2 and parts[0] == "SET":
                self.encoding = parts[1].strip()
                break

        text = _decode_bytes(raw_bytes, self.encoding)
        # First pass: settings (FLAG, FORBIDDENWORD, NEEDAFFIX, ONLYINCOMPOUND, COMPOUND*)
        lines = text.splitlines()
        for ln in lines:
            s = ln.strip()
            if not s or s.startswith("#"):
                continue
            parts = s.split()
            key = parts[0]
            if key == "FLAG" and len(parts) >= 2:
                fm = parts[1].lower()
                if fm in ("long", "num", "utf-8"):
                    self.flag_mode = fm
            elif key == "FORBIDDENWORD" and len(parts) >= 2:
                self.forbidden_flag = parts[1]
            elif key in ("NEEDAFFIX", "PSEUDOROOT") and len(parts) >= 2:
                self.needaffix_flag = parts[1]
            elif key == "ONLYINCOMPOUND" and len(parts) >= 2:
                self.onlyincompound_flag = parts[1]
            elif key == "COMPOUNDFLAG" and len(parts) >= 2:
                self.compound_flag = parts[1]
                self.has_compounding = True
            elif key == "COMPOUNDBEGIN" and len(parts) >= 2:
                self.compound_begin_flag = parts[1]
                self.has_compounding = True
            elif key == "COMPOUNDEND" and len(parts) >= 2:
                self.compound_end_flag = parts[1]
                self.has_compounding = True

        # Second pass: PFX / SFX rules
        cross_map: Dict[Tuple[str, str], bool] = {}
        for ln in lines:
            s = ln.strip()
            if not s or s.startswith("#"):
                continue
            parts = s.split()
            if len(parts) < 4 or parts[0] not in ("SFX", "PFX"):
                continue
            kind, flag = parts[0], parts[1]
            if len(parts) == 4 and parts[2] in ("Y", "N") and parts[3].isdigit():
                cross_map[(kind, flag)] = parts[2] == "Y"
                continue
            if len(parts) < 5:
                continue
            strip_raw, add_raw, cond = parts[2], parts[3], parts[4]
            strip = "" if strip_raw == "0" else strip_raw
            if "/" in add_raw:
                add_part, cont_raw = add_raw.split("/", 1)
                cont_flags = _parse_flags(cont_raw, self.flag_mode)
            else:
                add_part = add_raw
                cont_flags = ()
            add = "" if add_part == "0" else add_part
            # Ignore rules that inject spaces, hyphens, or apostrophes (our prompts are single alphabetic words)
            if any(ch in add for ch in (" ", "-", "'", ".")):
                continue
            is_sfx = kind == "SFX"
            rule = _AffixRule(
                flag=flag,
                cross=cross_map.get((kind, flag), False),
                strip=strip.lower(),
                add=add.lower(),
                cond_re=_compile_cond(cond.lower(), is_suffix=is_sfx),
                cont_flags=cont_flags,
            )
            if is_sfx:
                self.sfx_by_add.setdefault(rule.add, []).append(rule)
                if len(rule.add) > self.max_sfx_len:
                    self.max_sfx_len = len(rule.add)
            else:
                self.pfx_by_add.setdefault(rule.add, []).append(rule)
                if len(rule.add) > self.max_pfx_len:
                    self.max_pfx_len = len(rule.add)

    def _load_dic(self, path: Path) -> None:
        text = _decode_bytes(path.read_bytes(), self.encoding)
        lines = text.splitlines()
        start_idx = 1 if (lines and lines[0].strip().split()[0].isdigit()) else 0

        for ln in lines[start_idx:]:
            s = ln.strip()
            if not s or s.startswith("#"):
                continue
            # Strip trailing morphological fields separated by tab or space
            head = s.split("\t", 1)[0].split(" ", 1)[0]
            if not head:
                continue
            if "/" in head:
                word, raw_flags = head.split("/", 1)
                flags = set(_parse_flags(raw_flags, self.flag_mode))
            else:
                word = head
                flags = set()
            if not word or not word.isalpha():
                continue
            if self.forbidden_flag and self.forbidden_flag in flags:
                continue

            if word.islower():
                if self.onlyincompound_flag and self.onlyincompound_flag in flags:
                    self.compound_only_stems.add(word)
                    continue
                existing = self.lower_stems.get(word)
                if existing is None:
                    self.lower_stems[word] = flags
                else:
                    existing.update(flags)
            elif len(word) >= 2 and word[0].isupper() and word[1:].islower():
                if self.onlyincompound_flag and self.onlyincompound_flag in flags:
                    continue
                low = word.lower()
                existing = self.cap_stems.get(low)
                if existing is None:
                    self.cap_stems[low] = flags
                else:
                    existing.update(flags)

        # If a word exists as a normal lowercase entry, remove it from capitalized-only map
        for w in list(self.cap_stems):
            if w in self.lower_stems:
                del self.cap_stems[w]

    def _stem_bare_ok(self, w: str, stems: Dict[str, Set[str]]) -> bool:
        flags = stems.get(w)
        if flags is None:
            return False
        if self.needaffix_flag and self.needaffix_flag in flags:
            return False
        return True

    def _strip_one_sfx(
        self,
        w: str,
        stems: Dict[str, Set[str]],
        *,
        require_cross: bool = False,
        allow_cont: bool = True,
    ) -> bool:
        n = len(w)
        max_k = min(n - 1, self.max_sfx_len)
        for k in range(0, max_k + 1):
            tail = w[n - k :] if k > 0 else ""
            rules = self.sfx_by_add.get(tail)
            if not rules:
                continue
            base = w[: n - k]
            for r in rules:
                if require_cross and not r.cross:
                    continue
                cand = base + r.strip
                if not cand:
                    continue
                if r.cond_re is not None and not r.cond_re.search(cand):
                    continue
                c_flags = stems.get(cand)
                if c_flags is not None and r.flag in c_flags:
                    return True
                # 2-level suffix chaining (important for Hungarian/agglutinative dictionaries)
                if allow_cont and len(cand) >= 3:
                    if self._matches_with_required_flag(cand, r.flag, stems):
                        return True
        return False

    def _matches_with_required_flag(self, w: str, req_flag: str, stems: Dict[str, Set[str]]) -> bool:
        n = len(w)
        max_k = min(n - 1, self.max_sfx_len)
        for k in range(1, max_k + 1):
            tail = w[n - k :]
            rules = self.sfx_by_add.get(tail)
            if not rules:
                continue
            base = w[: n - k]
            for r in rules:
                if req_flag not in r.cont_flags:
                    continue
                cand = base + r.strip
                if not cand:
                    continue
                if r.cond_re is not None and not r.cond_re.search(cand):
                    continue
                c_flags = stems.get(cand)
                if c_flags is not None and r.flag in c_flags:
                    return True
        return False

    def _matches_stems(self, w: str, stems: Dict[str, Set[str]]) -> bool:
        if self._stem_bare_ok(w, stems):
            return True
        if self._strip_one_sfx(w, stems, allow_cont=True):
            return True

        # Prefix (+ optional cross-product suffix)
        n = len(w)
        max_p = min(n - 2, self.max_pfx_len)
        for k in range(1, max_p + 1):
            head = w[:k]
            rules = self.pfx_by_add.get(head)
            if not rules:
                continue
            rest = w[k:]
            for r in rules:
                cand = r.strip + rest
                if not cand:
                    continue
                if r.cond_re is not None and not r.cond_re.search(cand):
                    continue
                c_flags = stems.get(cand)
                if c_flags is not None and r.flag in c_flags:
                    return True
                if r.cross and self._strip_one_sfx(cand, stems, require_cross=True, allow_cont=False):
                    return True
        return False

    def _matches_compound(self, w: str) -> bool:
        if not self.has_compounding or len(w) < 6:
            return False
        left_flags = {f for f in (self.compound_flag, self.compound_begin_flag) if f}
        right_flags = {f for f in (self.compound_flag, self.compound_end_flag) if f}
        if not left_flags or not right_flags:
            return False
        n = len(w)
        for split in range(3, n - 2):
            left = w[:split]
            if left in self.compound_only_stems:
                left_ok = True
            else:
                lf = self.lower_stems.get(left)
                left_ok = bool(lf and (lf & left_flags))
            if not left_ok:
                continue
            right_full = w[split:]
            for link in ("", "s", "e", "en", "n"):
                if link and not right_full.startswith(link):
                    continue
                right = right_full[len(link) :]
                if len(right) < 3:
                    continue
                rf = self.lower_stems.get(right)
                if rf and (rf & right_flags):
                    return True
                if self._strip_one_sfx(right, self.lower_stems, allow_cont=False):
                    return True
        return False

    def classify(self, word: str) -> str:
        """Return `'lower'`, `'cap'`, or `'unknown'` for `word`."""
        w = word.strip().lower()
        if not w:
            return "unknown"
        hit = self._cache.get(w)
        if hit is not None:
            return hit
        if self._matches_stems(w, self.lower_stems) or self._matches_compound(w):
            res = "lower"
        elif self._matches_stems(w, self.cap_stems):
            res = "cap"
        else:
            res = "unknown"
        self._cache[w] = res
        return res
