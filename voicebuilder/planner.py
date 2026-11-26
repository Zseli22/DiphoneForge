"""Language-neutral hybrid real-word + minimal-pair prompt planner.

Wraps the proven greedy set-cover algorithm from `tools/plan_word_corpus.py`,
adds custom plain-text corpus frequency counting, optional duplicate-vowel
merging, and portable on-demand FrequencyWords download integration.
"""

from __future__ import annotations

import collections
import csv
import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .catalog import LanguageCatalog
from .downloader import CancelFn, ProgressFn, ensure_dictionary, ensure_wordlist
from .paths import get_app_root, get_espeak_paths, get_wordlists_cache_dir, tool_subprocess_cmd
from .project import VoiceProject

# Reuse battle-tested planning helpers directly from tools/plan_word_corpus.py
_TOOLS_DIR = get_app_root() / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))


@dataclass
class PlanSummary:
    voice: str
    vowel_count: int
    consonant_count: int
    word_count: int
    supplement_count: int
    total_prompts: int
    running_speech_pct: float
    target_diphones_hit: int
    target_diphones_total: int
    plan_dir: Path
    wordlist_path: Path


@dataclass
class ExtensionSummary:
    batch_id: str
    added_words: int
    first_line: int
    last_line: int
    total_prompts: int
    script_path: Path
    lines: List[int]
    target_takes: int = 2
    diphones_2plus_before: int = 0
    diphones_2plus_after: int = 0
    single_take_before: int = 0
    single_take_after: int = 0


@dataclass
class ReinforcementPreview:
    target_takes: int
    new_words: List[Dict[str, object]]
    diphones_2plus_before: int
    diphones_2plus_after: int
    single_take_before: int
    single_take_after: int
    est_minutes: int


def build_wordlist_from_text(source_path: Path, dest_path: Path, *, ascii_only: bool = False) -> Path:
    """Convert either a plain-text corpus OR an existing `word [count]` file into a clean frequency list."""
    raw_lines = source_path.read_text(encoding="utf-8", errors="replace").splitlines()
    # Detect if file is already `word count` format (check first 30 non-empty lines)
    non_empty = [ln.strip() for ln in raw_lines if ln.strip()][:30]
    already_freq = bool(non_empty) and all(
        len(ln.split()) == 2 and ln.split()[1].isdigit() for ln in non_empty
    )
    if already_freq:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_text("\n".join(raw_lines) + "\n", encoding="utf-8")
        return dest_path

    counts: collections.Counter[str] = collections.Counter()
    word_re = re.compile(r"[a-z]+") if ascii_only else re.compile(r"[^\W\d_]+", re.UNICODE)
    for ln in raw_lines:
        for tok in word_re.findall(ln.lower()):
            if 2 <= len(tok) <= 16:
                counts[tok] += 1
    if len(counts) < 50:
        raise ValueError(
            f"Custom text file {source_path.name} yielded only {len(counts)} unique words "
            "(need at least 50 words, ideally 2,000+ for good real-word coverage)."
        )
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    out_lines = [f"{w} {c}" for w, c in counts.most_common(50000)]
    dest_path.write_text("\n".join(out_lines) + "\n", encoding="utf-8")
    return dest_path


def generate_plan(
    project: VoiceProject,
    *,
    catalog: Optional[LanguageCatalog] = None,
    progress: Optional[ProgressFn] = None,
    cancel: Optional[CancelFn] = None,
) -> PlanSummary:
    """Generate `plan/` for `project` (auto-downloading wordlist if configured)."""
    if progress:
        progress("Resolving language and word list...", 0.05)

    cat = catalog or LanguageCatalog()
    lang = project.language_spec(cat)
    espeak_bin, espeak_data = get_espeak_paths()
    if not espeak_bin.is_file():
        raise FileNotFoundError(f"Bundled eSpeak NG not found at {espeak_bin}")

    # 1. Resolve word list (auto-download FrequencyWords or import custom text/list)
    wl_src = project.config.wordlist_source.strip()
    if wl_src == "auto":
        wl_path = ensure_wordlist(lang, catalog=cat, progress=progress, cancel=cancel)
    else:
        src_file = Path(wl_src).expanduser()
        if not src_file.is_file():
            raise FileNotFoundError(f"Custom word list / text file not found: {src_file}")
        wl_path = build_wordlist_from_text(
            src_file,
            project.plan_dir / "custom_wordlist.txt",
            ascii_only=lang.ascii_only,
        )

    if cancel and cancel():
        from .downloader import CancelledError
        raise CancelledError("Plan generation cancelled.")

    # 1b. Resolve on-demand Hunspell dictionary for language authenticity filtering
    dict_path = ensure_dictionary(lang, catalog=cat, progress=progress, cancel=cancel)
    en_wl_fallback = get_wordlists_cache_dir() / "en_50k.txt"

    if progress:
        progress(f"Scanning vocabulary with eSpeak NG ({lang.espeak_voice})...", 0.25)

    # 2. Run tools/plan_word_corpus.py cleanly as a subprocess (zero stdout/global side effects)
    compare_voice = project.config.compare_voice or "none"
    skip_symbols = ",".join(project.config.skip_symbols) if project.config.skip_symbols else "none"
    supp_mode = "real-gaps" if getattr(project.config, "include_supplements", True) else "none"

    cmd = [
        *tool_subprocess_cmd("plan_word_corpus"),
        "--voice", project.config.espeak_voice,
        "--compare-voice", compare_voice,
        "--skip-symbols", skip_symbols,
        "--wordlist", str(wl_path),
        "--out-dir", str(project.plan_dir),
        "--max-words", str(int(getattr(project.config, "max_words", 0) or 0)),
        "--min-takes", str(getattr(project.config, "min_takes", 1)),
        "--supplement-mode", supp_mode,
        "--espeak-bin", str(espeak_bin),
        "--espeak-data", str(espeak_data),
    ]
    if dict_path is not None and dict_path.is_file():
        cmd.extend(["--dict-file", str(dict_path)])
    elif not lang.espeak_voice.lower().startswith("en") and en_wl_fallback.is_file():
        cmd.extend(["--english-wordlist", str(en_wl_fallback)])
    if not getattr(project.config, "merge_duplicate_vowels", True):
        cmd.append("--no-merge-ipa-dups")
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    res = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=creationflags,
    )
    if res.returncode != 0:
        raise RuntimeError(f"Prompt planner failed:\n{res.stdout}\n{res.stderr}")

    # 3. Parse generated artifacts and enrich inventory with language catalog metadata
    inv_path = project.plan_dir / "inventory.json"
    dmap_path = project.plan_dir / "diphone_map.json"
    curve_path = project.plan_dir / "coverage_curve.csv"

    inv = json.loads(inv_path.read_text(encoding="utf-8"))
    inv["language_id"] = lang.id
    inv["espeak_phonemes"] = project.config.espeak_phonemes
    inv["espeak_lang_lines"] = list(project.config.espeak_lang_lines)
    inv_path.write_text(json.dumps(inv, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    dmap = json.loads(dmap_path.read_text(encoding="utf-8"))
    running_pct = 0.0
    target_hit = 0
    target_total = 0
    if curve_path.is_file():
        with curve_path.open("r", encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
        if rows:
            last = rows[-1]
            running_pct = 100.0 * float(last.get("token_coverage", 0.0))
            target_hit = int(last.get("cum_diphones", 0))
            type_cov = float(last.get("type_coverage", 0.0))
            target_total = int(round(target_hit / type_cov)) if type_cov > 0 else target_hit

    project.set_stage("plan", "done")
    if progress:
        progress(
            f"Plan ready: {dmap['word_count']} words + {dmap['supplement_count']} supplements "
            f"({running_pct:.1f}% running speech).",
            1.0,
        )

    return PlanSummary(
        voice=project.config.espeak_voice,
        vowel_count=len(inv["vowels"]),
        consonant_count=len(inv["consonants"]),
        word_count=int(dmap["word_count"]),
        supplement_count=int(dmap["supplement_count"]),
        total_prompts=len(dmap["lines"]),
        running_speech_pct=round(running_pct, 2),
        target_diphones_hit=target_hit,
        target_diphones_total=target_total,
        plan_dir=project.plan_dir,
        wordlist_path=wl_path,
    )


def compute_reinforcement_pass(
    project: VoiceProject,
    *,
    extra_words: int = 0,
    min_takes: int = 2,
    catalog: Optional[LanguageCatalog] = None,
    progress: Optional[ProgressFn] = None,
    cancel: Optional[CancelFn] = None,
) -> ReinforcementPreview:
    """Calculate the exact words needed to reinforce the lowest-take core pairs without modifying the plan."""
    dmap_path = project.plan_dir / "diphone_map.json"
    if not dmap_path.is_file():
        raise FileNotFoundError(f"Generate an initial plan first (missing {dmap_path}).")

    if progress:
        progress("Analyzing current diphone take counts...", 0.10)

    cat = catalog or LanguageCatalog()
    lang = project.language_spec(cat)
    espeak_bin, espeak_data = get_espeak_paths()
    if not espeak_bin.is_file():
        raise FileNotFoundError(f"Bundled eSpeak NG not found at {espeak_bin}")

    wl_src = project.config.wordlist_source.strip()
    if wl_src == "auto":
        wl_path = ensure_wordlist(lang, catalog=cat, progress=progress, cancel=cancel)
    else:
        src_file = Path(wl_src).expanduser()
        if not src_file.is_file():
            raise FileNotFoundError(f"Custom word list / text file not found: {src_file}")
        wl_path = build_wordlist_from_text(
            src_file,
            project.plan_dir / "custom_wordlist.txt",
            ascii_only=lang.ascii_only,
        )

    dict_path = ensure_dictionary(lang, catalog=cat, progress=progress, cancel=cancel)
    en_wl_fallback = get_wordlists_cache_dir() / "en_50k.txt"

    if cancel and cancel():
        from .downloader import CancelledError
        raise CancelledError("Reinforcement calculation cancelled.")

    dmap = json.loads(dmap_path.read_text(encoding="utf-8"))
    inv_path = project.plan_dir / "inventory.json"
    allowed_phones = None
    if inv_path.is_file():
        inv_doc = json.loads(inv_path.read_text(encoding="utf-8"))
        allowed_phones = set(inv_doc.get("vowels", [])) | set(inv_doc.get("consonants", []))

    existing_lines: List[Dict[str, object]] = list(dmap.get("lines", []))
    dp_counts_before: collections.Counter[str] = collections.Counter()
    for it in existing_lines:
        phs = list(it.get("phones", []))
        for a, b in zip(phs, phs[1:]):
            dp_counts_before[f"{a}_{b}"] += 1
    diphones_2plus_before = sum(1 for c in dp_counts_before.values() if c >= 2)
    single_take_before = sum(1 for c in dp_counts_before.values() if c == 1)

    target_takes = max(2, int(min_takes))
    if progress:
        progress(f"Calculating reinforcement words targeting {target_takes}+ takes for lowest-take core pairs...", 0.35)

    ext_tmp = project.plan_dir / "_extend_tmp"
    compare_voice = project.config.compare_voice or "none"
    skip_symbols = ",".join(project.config.skip_symbols) if project.config.skip_symbols else "none"
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

    def _run_extend(k_takes: int) -> List[Dict[str, object]]:
        if ext_tmp.exists():
            shutil.rmtree(ext_tmp)
        cmd = [
            *tool_subprocess_cmd("plan_word_corpus"),
            "--voice", project.config.espeak_voice,
            "--compare-voice", compare_voice,
            "--skip-symbols", skip_symbols,
            "--wordlist", str(wl_path),
            "--out-dir", str(ext_tmp),
            "--extend-from", str(project.plan_dir),
            "--max-words", str(int(extra_words or 0)),
            "--min-takes", str(k_takes),
            "--supplement-mode", "none",
            "--espeak-bin", str(espeak_bin),
            "--espeak-data", str(espeak_data),
        ]
        if dict_path is not None and dict_path.is_file():
            cmd.extend(["--dict-file", str(dict_path)])
        elif not lang.espeak_voice.lower().startswith("en") and en_wl_fallback.is_file():
            cmd.extend(["--english-wordlist", str(en_wl_fallback)])
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
        if res.returncode != 0:
            raise RuntimeError(f"Reinforcement planner failed:\n{res.stdout}\n{res.stderr}")
        ext_dmap = json.loads((ext_tmp / "diphone_map.json").read_text(encoding="utf-8"))
        words = [dict(it) for it in ext_dmap.get("lines", []) if it.get("kind") == "word"]
        if allowed_phones is not None:
            words = [w for w in words if all(p in allowed_phones for p in w.get("phones", []))]
        return words

    try:
        new_words = _run_extend(target_takes)
        while not new_words and target_takes < 4:
            target_takes += 1
            if progress:
                progress(f"Core pairs already meet {target_takes - 1}x takes; calculating {target_takes}x pass...", 0.60)
            new_words = _run_extend(target_takes)
    finally:
        shutil.rmtree(ext_tmp, ignore_errors=True)

    if not new_words:
        raise RuntimeError("No additional words are needed — core sound pairs already meet the redundancy goal.")

    dp_counts_after = collections.Counter(dp_counts_before)
    for item in new_words:
        phs = list(item.get("phones", []))
        for a, b in zip(phs, phs[1:]):
            dp_counts_after[f"{a}_{b}"] += 1
    diphones_2plus_after = sum(1 for c in dp_counts_after.values() if c >= 2)
    single_take_after = sum(1 for c in dp_counts_after.values() if c == 1)
    est_min = max(1, int(round(len(new_words) * 2.0 / 60.0)))

    if progress:
        progress(f"Calculated {len(new_words)} reinforcement words (~{est_min} min recording).", 1.0)

    return ReinforcementPreview(
        target_takes=target_takes,
        new_words=new_words,
        diphones_2plus_before=diphones_2plus_before,
        diphones_2plus_after=diphones_2plus_after,
        single_take_before=single_take_before,
        single_take_after=single_take_after,
        est_minutes=est_min,
    )


def apply_reinforcement_pass(project: VoiceProject, preview: ReinforcementPreview) -> ExtensionSummary:
    """Commit a computed `ReinforcementPreview` onto `project`'s plan without altering earlier lines."""
    dmap_path = project.plan_dir / "diphone_map.json"
    dmap = json.loads(dmap_path.read_text(encoding="utf-8"))
    existing_lines: List[Dict[str, object]] = list(dmap.get("lines", []))
    first_line = max((int(x["line"]) for x in existing_lines), default=0) + 1

    appended_lines: List[int] = []
    appended_script_rows: List[str] = []
    for offset, raw_item in enumerate(preview.new_words):
        item = dict(raw_item)
        ln = first_line + offset
        item["line"] = ln
        existing_lines.append(item)
        appended_lines.append(ln)
        appended_script_rows.append(f"{ln:04d}  {item['text']}")

    dmap["lines"] = existing_lines
    dmap["word_count"] = int(dmap.get("word_count", 0)) + len(preview.new_words)

    tmp_dmap = dmap_path.with_suffix(".json.tmp")
    tmp_dmap.write_text(json.dumps(dmap, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp_dmap.replace(dmap_path)

    full_script = project.plan_dir / "recording_script.txt"
    existing_txt = full_script.read_text(encoding="utf-8").rstrip("\n") if full_script.is_file() else ""
    combined_txt = (existing_txt + "\n" if existing_txt else "") + "\n".join(appended_script_rows) + "\n"
    tmp_script = full_script.with_suffix(".txt.tmp")
    tmp_script.write_text(combined_txt, encoding="utf-8")
    tmp_script.replace(full_script)

    project.pickups_dir.mkdir(parents=True, exist_ok=True)
    ext_idx = sum(1 for pb in project.config.pickups if str(pb.get("id", "")).startswith("ext_")) + 1
    batch_id = f"ext_{ext_idx:02d}"
    ext_script_lines = [
        f"# DiphoneForge Reinforcement Pass — {batch_id} ({len(preview.new_words)} new words: lines {first_line}-{appended_lines[-1]})",
        "# Record ONLY these new words in order with a brief pause (~0.5 s) between each word.",
        "# Your earlier recordings and alignments remain completely untouched.",
        "",
    ]
    for idx, (ln, item) in enumerate(zip(appended_lines, preview.new_words), 1):
        ext_script_lines.append(f"{idx:03d}  (Line {ln:04d})  {item['text']}")
    ext_script_path = project.pickups_dir / f"{batch_id}_script.txt"
    ext_script_path.write_text("\n".join(ext_script_lines) + "\n", encoding="utf-8")

    meta_doc = {
        "id": batch_id,
        "kind": "extension",
        "lines": appended_lines,
        "script": f"pickups/{ext_script_path.name}",
        "applied": False,
    }
    (project.pickups_dir / f"{batch_id}_meta.json").write_text(
        json.dumps(meta_doc, indent=2) + "\n", encoding="utf-8"
    )
    project.config.pickups.append(meta_doc)
    project.save()

    return ExtensionSummary(
        batch_id=batch_id,
        added_words=len(preview.new_words),
        first_line=first_line,
        last_line=appended_lines[-1],
        total_prompts=len(existing_lines),
        script_path=ext_script_path,
        lines=appended_lines,
        target_takes=preview.target_takes,
        diphones_2plus_before=preview.diphones_2plus_before,
        diphones_2plus_after=preview.diphones_2plus_after,
        single_take_before=preview.single_take_before,
        single_take_after=preview.single_take_after,
    )


def extend_project_plan(
    project: VoiceProject,
    *,
    extra_words: int = 0,
    min_takes: int = 2,
    catalog: Optional[LanguageCatalog] = None,
    progress: Optional[ProgressFn] = None,
    cancel: Optional[CancelFn] = None,
) -> ExtensionSummary:
    """Auto-calculate and append a reinforcement pass to an existing plan without altering earlier lines."""
    preview = compute_reinforcement_pass(
        project,
        extra_words=extra_words,
        min_takes=min_takes,
        catalog=catalog,
        progress=progress,
        cancel=cancel,
    )
    return apply_reinforcement_pass(project, preview)
