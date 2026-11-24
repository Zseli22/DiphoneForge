"""Speaker- and language-neutral MBROLA database & eSpeak voice builder wrapper.

Drives `tools/build_word_mbrola_db.py` while enforcing verified per-language
eSpeak phoneme tables (`espeak_phonemes` & `espeak_lang_lines`) from the
language catalog and updating `project.json` with measured speaker F0 and
build summaries.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional

from .downloader import CancelFn, CancelledError, ProgressFn
from .paths import get_app_root, get_mbrolator_binaries, tool_subprocess_cmd
from .project import VoiceProject

_TOOLS_DIR = get_app_root() / "tools"
_RE_ANALYZED = re.compile(r"analyzed\s+(\d+)\s*/\s*(\d+)\s+units", re.IGNORECASE)
_RE_COVERAGE_PHO = re.compile(r"coverage_(\d+)\.pho", re.IGNORECASE)


def _db_name_for_rate(voice_id: str, rate: int, rates: List[int]) -> str:
    """Derive clean voice name for each target rate (e.g. `ukd` at 22.05 kHz, `ukd16` at 16 kHz)."""
    base = voice_id[:-2] if (voice_id.endswith("16") and len(voice_id) > 2) else voice_id
    if rate == 22050:
        return base
    if rate == 16000:
        if len(rates) == 1 and not voice_id.endswith("16"):
            return voice_id
        return f"{base}16"
    return voice_id


def _estimate_build_subprogress(line: str, current: float) -> float:
    """Map `build_word_mbrola_db.py` stdout milestones to a monotonic 0.0..1.0 fraction."""
    m = _RE_ANALYZED.search(line)
    if m:
        done, total = int(m.group(1)), max(1, int(m.group(2)))
        return max(current, 0.10 + 0.72 * min(1.0, done / float(total)))
    m_cov = _RE_COVERAGE_PHO.search(line)
    if m_cov:
        idx = int(m_cov.group(1))
        return max(current, min(0.98, 0.91 + 0.01 * (idx + 1)))
    low = line.lower()
    if low.startswith("unit grid:"):
        return max(current, 0.04)
    if low.startswith("selected "):
        return max(current, 0.06)
    if low.startswith("resampling "):
        return max(current, 0.08)
    if low.startswith("matrix units:"):
        return max(current, 0.10)
    if low.startswith("gain="):
        return max(current, 0.84)
    if "packing database" in low:
        return max(current, 0.86)
    if low.startswith("packed "):
        return max(current, 0.89)
    if "validating all units" in low:
        return max(current, 0.90)
    if "wrote espeak voice files" in low:
        return max(current, 0.99)
    return current


def _rewrite_verified_descriptor(
    project: VoiceProject,
    voice_dir: Path,
    db_name: str,
    phtrans_name: str,
    rate: int,
    f0_median_hz: float,
) -> Path:
    """Write `mb-<db_name>` using exact catalog `espeak_phonemes` and `espeak_lang_lines`."""
    pitch_low = max(55, int(round(f0_median_hz * 0.78)))
    pitch_high = max(pitch_low + 25, int(round(f0_median_hz * 1.15)))
    gender = "female" if f0_median_hz >= 165.0 else "male"
    lang_block = "\n".join(project.config.espeak_lang_lines)
    desc = (
        f"name {db_name}\n"
        f"{lang_block}\n"
        f"gender {gender}\n"
        f"\n"
        f"phonemes {project.config.espeak_phonemes}\n"
        f"pitch {pitch_low} {pitch_high}\n"
        f"mbrola {db_name} {phtrans_name} {rate}\n"
    )
    desc_path = voice_dir / f"mb-{db_name}"
    desc_path.write_text(desc, encoding="utf-8")
    return desc_path


def refresh_unit_grid(
    project: VoiceProject,
    *,
    progress: Optional[ProgressFn] = None,
) -> Path:
    """Fast (~2 s) rebuild of `build/unit_grid.csv` from all currently aligned lines.

    Lets the user open the Interactive Take Picker immediately after alignment or
    after importing a corpus extension recording (before running the full MBROLA build).
    """
    import csv

    if not project.segments_csv.is_file():
        raise FileNotFoundError(f"Missing segments.csv (run alignment first): {project.segments_csv}")
    if not project.token_meta_file.is_file():
        raise FileNotFoundError(f"Missing token_meta.json (run alignment first): {project.token_meta_file}")

    if str(_TOOLS_DIR) not in sys.path:
        sys.path.insert(0, str(_TOOLS_DIR))
    import build_word_mbrola_db as B

    if progress:
        progress("Refreshing unit grid and scoring all aligned diphone takes...", 0.25)

    inv = json.loads((project.plan_dir / "inventory.json").read_text(encoding="utf-8"))
    dmap_doc = json.loads((project.plan_dir / "diphone_map.json").read_text(encoding="utf-8"))
    dmap_by_line = {int(x["line"]): x for x in dmap_doc["lines"]}
    token_meta = json.loads(project.token_meta_file.read_text(encoding="utf-8"))
    with project.segments_csv.open("r", encoding="utf-8", newline="") as f:
        segments = {int(r["line"]): r for r in csv.DictReader(f)}

    out_csv = project.build_dir / "unit_grid.csv"
    rows, f0_stats = B.build_unit_grid(
        segments=segments,
        token_meta=token_meta,
        diphone_map=dmap_by_line,
        aligned_dir=project.mfa_aligned_dir,
        audio_dir=project.recordings_dir,
        vowels=set(inv["vowels"]),
        out_csv=out_csv,
    )
    project.config.speaker_f0_hz = round(float(f0_stats["median_hz"]), 2)
    project.save()
    if progress:
        progress(f"Unit grid refreshed ({len(rows)} candidate takes across {len(segments)} aligned lines).", 1.0)
    return out_csv


def build_voice_databases(
    project: VoiceProject,
    *,
    jobs: Optional[int] = None,
    progress: Optional[ProgressFn] = None,
    cancel: Optional[CancelFn] = None,
) -> List[Dict[str, object]]:
    """Build MBROLA voice database(s) for all configured sample rates in `project.config.rates`."""
    bins = get_mbrolator_binaries()
    missing = [k for k, p in bins.items() if not p.is_file()]
    if missing:
        raise FileNotFoundError(f"Missing MBROLATOR/MBROLA binaries: {', '.join(missing)}")

    if not project.segments_csv.is_file():
        raise FileNotFoundError(f"Missing segments.csv (run alignment first): {project.segments_csv}")
    if not project.token_meta_file.is_file():
        raise FileNotFoundError(f"Missing token_meta.json (run alignment first): {project.token_meta_file}")

    n_jobs = jobs or max(1, min(8, (os.cpu_count() or 4) - 1))
    rates = list(project.config.rates or [22050])
    summaries: List[Dict[str, object]] = []

    for r_idx, rate in enumerate(rates):
        if cancel and cancel():
            raise CancelledError("Voice build cancelled by user.")

        db_name = _db_name_for_rate(project.config.voice_id, rate, rates)
        phtrans_name = f"{db_name}_phtrans"
        rate_base = r_idx / float(len(rates))
        rate_span = 1.0 / float(len(rates))
        sub_frac = 0.01
        if progress:
            progress(f"Building {db_name} ({rate} Hz)...", rate_base + rate_span * sub_frac)

        cmd = [
            *tool_subprocess_cmd("build_word_mbrola_db"),
            "--plan-dir", str(project.plan_dir),
            "--token-meta", str(project.token_meta_file),
            "--segments-csv", str(project.segments_csv),
            "--aligned-dir", str(project.mfa_aligned_dir),
            "--audio-dir", str(project.recordings_dir),
            "--out-dir", str(project.build_dir),
            "--name", db_name,
            "--phtrans-name", phtrans_name,
            "--rate", str(rate),
            "--jobs", str(n_jobs),
        ]
        ov_file = project.build_dir / "take_overrides.json"
        if ov_file.is_file():
            cmd.extend(["--take-overrides", str(ov_file)])
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
        tail: List[str] = []
        assert proc.stdout is not None
        try:
            for line in proc.stdout:
                if cancel and cancel():
                    proc.terminate()
                    raise CancelledError("Voice build cancelled by user.")
                s = line.strip()
                if s:
                    tail.append(s)
                    if len(tail) > 40:
                        tail.pop(0)
                    sub_frac = _estimate_build_subprogress(s, sub_frac)
                    if progress:
                        progress(f"[{db_name}] {s[:110]}", rate_base + rate_span * sub_frac)
            rc = proc.wait()
        except BaseException:
            proc.terminate()
            raise

        if rc != 0:
            raise RuntimeError(f"MBROLA build failed for {db_name} (exit {rc}):\n" + "\n".join(tail[-20:]))

        summary_path = project.build_dir / f"build_summary_{db_name}.json"
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        f0_med = float(summary["speaker_f0"]["median_hz"])
        voice_dir = project.build_dir / db_name
        _rewrite_verified_descriptor(project, voice_dir, db_name, phtrans_name, rate, f0_med)

        project.config.speaker_f0_hz = round(f0_med, 2)
        summaries.append(summary)

    project.set_stage("build", "done")
    if progress:
        progress(f"Build complete: {', '.join(str(s['name']) for s in summaries)}", 1.0)
    return summaries
