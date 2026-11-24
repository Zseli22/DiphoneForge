"""Live speech preview (via 32-bit espeakMbrolaHost.exe), standalone export, and NVDA add-on install."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

from .paths import find_nvda_addon_dir, find_preview_engine_dir, get_app_root, tool_subprocess_cmd
from .project import VoiceProject

_TOOLS_DIR = get_app_root() / "tools"
SHIPPED_PROTECTED_VOICES = {"usd16", "usd"}


def _ensure_project_preview_stage(project: VoiceProject) -> Path:
    """Create an isolated preview engine copy under `<project>/samples/_engine_stage`."""
    src = find_preview_engine_dir()
    if src is None:
        raise FileNotFoundError(
            "32-bit espeakMbrolaHost.exe engine not found. Build the NVDA add-on (`build.ps1`) "
            "or install the espeakWitMbrola add-on in NVDA to enable live preview."
        )
    stage = project.samples_dir / "_engine_stage"
    if not (stage / "espeakMbrolaHost.exe").is_file():
        if stage.exists():
            shutil.rmtree(stage)
        shutil.copytree(src, stage)
    return stage


def synthesize_preview(
    project: VoiceProject,
    db_name: str,
    text: str,
    out_wav: Optional[Path] = None,
) -> Path:
    """Synthesize `text` through `espeakMbrolaHost.exe` using the project's built `db_name`."""
    voice_dir = project.build_dir / db_name
    db_file = voice_dir / db_name
    phtrans_file = voice_dir / f"{db_name}_phtrans"
    desc_file = voice_dir / f"mb-{db_name}"
    for p in (db_file, phtrans_file, desc_file):
        if not p.is_file():
            raise FileNotFoundError(f"Built voice file missing (run build first): {p}")

    stage = _ensure_project_preview_stage(project)
    data = stage / "espeak-ng-data"
    (data / "mbrola").mkdir(parents=True, exist_ok=True)
    (data / "mbrola_ph").mkdir(parents=True, exist_ok=True)
    (data / "voices" / "mb").mkdir(parents=True, exist_ok=True)

    shutil.copy2(db_file, data / "mbrola" / db_name)
    shutil.copy2(phtrans_file, data / "mbrola_ph" / phtrans_file.name)
    shutil.copy2(desc_file, data / "voices" / "mb" / desc_file.name)

    dest_wav = out_wav or (project.samples_dir / f"preview_{db_name}.wav")
    dest_wav.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        *tool_subprocess_cmd("smoke_mbrola_host"),
        "--engine-dir", str(stage),
        "--host", str(stage / "espeakMbrolaHost.exe"),
        "--voice", f"mb-{db_name}",
        "--text", text,
        "--output", str(dest_wav),
    ]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    res = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", creationflags=creationflags)
    if res.returncode != 0 or not dest_wav.is_file():
        raise RuntimeError(f"Preview synthesis failed:\n{res.stdout}\n{res.stderr}")
    return dest_wav


def export_voice_package(project: VoiceProject, db_name: str, export_dir: Path) -> Path:
    """Export a clean, distributable voice package folder (`db`, `phtrans`, `mb-<id>`, `manifest.json`, `PROVENANCE.md`)."""
    voice_dir = project.build_dir / db_name
    target = export_dir / db_name
    target.mkdir(parents=True, exist_ok=True)

    for fname in (db_name, f"{db_name}_phtrans", f"mb-{db_name}", "manifest.json"):
        src = voice_dir / fname
        if src.is_file():
            shutil.copy2(src, target / fname)
    summary_src = project.build_dir / f"build_summary_{db_name}.json"
    if summary_src.is_file():
        shutil.copy2(summary_src, target / summary_src.name)

    prov = target / "PROVENANCE.md"
    if not prov.exists():
        prov.write_text(
            f"# Voice Package: `{db_name}`\n\n"
            f"- **Language:** `{project.config.language_id}` (`espeak-ng` voice `{project.config.espeak_voice}`)\n"
            f"- **Speaker F0 (median):** {project.config.speaker_f0_hz or 'unknown'} Hz\n"
            f"- **Built with:** DiphoneForge Voice Builder\n"
            f"- **Speaker consent & license:** *(Fill in speaker attribution and chosen license, e.g. CC BY 4.0, before publishing.)*\n",
            encoding="utf-8",
        )
    return target


def install_voice_to_nvda(
    project: VoiceProject,
    db_name: str,
    *,
    nvda_addon_dir: Optional[Path] = None,
    allow_overwrite_shipped: bool = False,
) -> Path:
    """Install a built voice into NVDA's `espeakWitMbrola` add-on without touching shipped files."""
    if db_name in SHIPPED_PROTECTED_VOICES and not allow_overwrite_shipped:
        raise ValueError(f"Refusing to overwrite shipped voice {db_name!r}. Choose a different voice ID.")

    addon_dir = nvda_addon_dir or find_nvda_addon_dir()
    if addon_dir is None:
        raise FileNotFoundError(
            "Could not find installed NVDA add-on at %APPDATA%\\nvda\\addons\\espeakWitMbrola\\synthDrivers."
        )
    voice_dir = project.build_dir / db_name
    data = addon_dir / "espeak-ng-data"
    (data / "mbrola").mkdir(parents=True, exist_ok=True)
    (data / "mbrola_ph").mkdir(parents=True, exist_ok=True)
    (data / "voices" / "mb").mkdir(parents=True, exist_ok=True)

    shutil.copy2(voice_dir / db_name, data / "mbrola" / db_name)
    shutil.copy2(voice_dir / f"{db_name}_phtrans", data / "mbrola_ph" / f"{db_name}_phtrans")
    shutil.copy2(voice_dir / f"mb-{db_name}", data / "voices" / "mb" / f"mb-{db_name}")
    return data
