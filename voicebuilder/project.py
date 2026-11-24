"""Self-contained voice project model (`project.json`) and directory contract."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

from .catalog import LanguageCatalog, LanguageSpec


SCHEMA_VERSION = 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def validate_voice_id(voice_id: str) -> str:
    cleaned = voice_id.strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,15}", cleaned):
        raise ValueError(
            f"Invalid voice ID {voice_id!r}: use 2-16 lowercase letters, digits, or underscores starting with a letter."
        )
    return cleaned


@dataclass
class PickupBatch:
    id: str
    lines: List[int]
    wav_files: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=_utc_now)
    applied: bool = False


@dataclass
class ProjectConfig:
    schema_version: int
    voice_id: str
    language_id: str
    espeak_voice: str
    espeak_phonemes: str
    espeak_lang_lines: List[str]
    mfa_model: str
    wordlist_source: str  # "auto" or path to custom wordlist/text file
    rates: List[int] = field(default_factory=lambda: [22050])
    recording_mode: str = "mic"  # "mic" or "external"
    max_words: int = 500
    min_takes: int = 1
    supplement_mode: str = "real-gaps"
    merge_duplicate_vowels: bool = True
    compare_voice: Optional[str] = None
    skip_symbols: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=_utc_now)
    updated_at: str = field(default_factory=_utc_now)
    stages: Dict[str, str] = field(
        default_factory=lambda: {
            "plan": "pending",
            "record": "pending",
            "align": "pending",
            "build": "pending",
        }
    )
    speaker_f0_hz: Optional[float] = None
    pickups: List[Dict[str, object]] = field(default_factory=list)


class VoiceProject:
    """Manages a portable voice-building project folder and its `project.json`."""

    def __init__(self, root: Path, config: ProjectConfig) -> None:
        self.root = root.resolve()
        self.config = config

    @property
    def project_file(self) -> Path:
        return self.root / "project.json"

    @property
    def plan_dir(self) -> Path:
        return self.root / "plan"

    @property
    def recordings_dir(self) -> Path:
        return self.root / "recordings"

    @property
    def takes_dir(self) -> Path:
        return self.recordings_dir / "takes"

    @property
    def external_dir(self) -> Path:
        return self.recordings_dir / "external"

    @property
    def pickups_dir(self) -> Path:
        return self.recordings_dir / "pickups"

    @property
    def ranges_file(self) -> Path:
        return self.external_dir / "ranges.txt"

    @property
    def align_dir(self) -> Path:
        return self.root / "align"

    @property
    def segments_dir(self) -> Path:
        return self.align_dir / "segments"

    @property
    def segments_csv(self) -> Path:
        return self.segments_dir / "segments.csv"

    @property
    def mfa_aligned_dir(self) -> Path:
        return self.align_dir / "mfa_aligned"

    @property
    def token_meta_file(self) -> Path:
        return self.align_dir / "token_meta.json"

    @property
    def audit_file(self) -> Path:
        return self.align_dir / "audit.json"

    @property
    def build_dir(self) -> Path:
        return self.root / "build"

    @property
    def samples_dir(self) -> Path:
        return self.root / "samples"

    def ensure_dirs(self) -> None:
        for d in (
            self.root,
            self.plan_dir,
            self.takes_dir,
            self.external_dir,
            self.pickups_dir,
            self.align_dir,
            self.segments_dir,
            self.mfa_aligned_dir,
            self.build_dir,
            self.samples_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

    def save(self) -> None:
        self.ensure_dirs()
        self.config.updated_at = _utc_now()
        tmp = self.project_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(self.config), indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        tmp.replace(self.project_file)

    def set_stage(self, stage: str, status: str) -> None:
        self.config.stages[stage] = status
        self.save()

    @classmethod
    def create(
        cls,
        project_dir: Path,
        voice_id: str,
        lang: LanguageSpec,
        *,
        rates: Optional[List[int]] = None,
        wordlist_source: str = "auto",
        recording_mode: str = "mic",
        max_words: int = 500,
        supplement_mode: str = "real-gaps",
        merge_duplicate_vowels: bool = True,
    ) -> "VoiceProject":
        vid = validate_voice_id(voice_id)
        cfg = ProjectConfig(
            schema_version=SCHEMA_VERSION,
            voice_id=vid,
            language_id=lang.id,
            espeak_voice=lang.espeak_voice,
            espeak_phonemes=lang.espeak_phonemes,
            espeak_lang_lines=list(lang.espeak_lang_lines),
            mfa_model=lang.mfa_model,
            wordlist_source=wordlist_source,
            rates=list(rates or [22050]),
            recording_mode=recording_mode,
            max_words=max_words,
            supplement_mode=supplement_mode,
            merge_duplicate_vowels=merge_duplicate_vowels,
            compare_voice=lang.compare_voice,
            skip_symbols=list(lang.skip_symbols),
        )
        proj = cls(project_dir, cfg)
        proj.save()
        return proj

    @classmethod
    def load(cls, project_dir: Path) -> "VoiceProject":
        root = project_dir.resolve()
        pf = root / "project.json"
        if not pf.is_file():
            raise FileNotFoundError(f"Not a DiphoneForge project (missing project.json): {root}")
        raw = json.loads(pf.read_text(encoding="utf-8"))
        cfg = ProjectConfig(**raw)
        return cls(root, cfg)

    def language_spec(self, catalog: Optional[LanguageCatalog] = None) -> LanguageSpec:
        cat = catalog or LanguageCatalog()
        return cat.get(self.config.language_id)
