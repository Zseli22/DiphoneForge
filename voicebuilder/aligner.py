"""MFA forced alignment, VAD integrity audit, built-in take export, and partial external pickup patching.

Supports three unified paths converging on `<project>/align/segments/` + `segments.csv`
+ `<project>/align/mfa_aligned/`:
  1. Built-in Mic Studio takes (`recordings/takes/line_XXXX.wav`) -> Pass-2 MFA directly.
  2. External Recorder long WAV(s) + strict range map (`recordings/external/ranges.txt`)
     -> Pass-1 MFA -> VAD integrity check -> outer-silence trim -> slice -> Pass-2 MFA.
  3. Partial External Pickup Patching (`export_pickup_script` + `apply_pickup_recording`):
     aligns & slices ONLY a short pickup WAV covering flagged lines (e.g. [142, 319, 412])
     and patches ONLY those rows in `segments.csv` + `mfa_aligned/` while leaving all
     untouched lines and original recordings 100% intact.
"""

from __future__ import annotations

import csv
import json
import math
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

from .downloader import (
    CancelFn,
    CancelledError,
    ProgressFn,
    ensure_mfa_env,
    ensure_mfa_model,
    inspect_mfa_model_phones,
)
from .paths import get_app_root, mfa_subprocess_env
from .phone_map import ENGLISH_MFA_VARIANTS, PhoneMapResult, build_phone_map
from .project import VoiceProject

_TOOLS_DIR = get_app_root() / "tools"
if str(_TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(_TOOLS_DIR))
from resegment_from_whole_alignment import parse_textgrid  # noqa: E402


@dataclass
class AuditFlag:
    line: int
    text: str
    kind: str
    reasons: List[str]
    source_wav: str = ""
    clip_wav: str = ""


@dataclass
class AlignmentReport:
    total_lines: int
    aligned_lines: int
    phone_ok_lines: int
    flags: List[AuditFlag]
    phone_map_approximate: List[Tuple[str, str, List[str]]]


def parse_line_list(spec: str) -> List[int]:
    """Parse a user-friendly line subset string like `'142, 319, 410-412'` into ordered ints."""
    out: List[int] = []
    seen = set()
    for part in re.split(r"[\s,;]+", spec.strip()):
        if not part:
            continue
        if "-" in part:
            a_s, b_s = part.split("-", 1)
            a, b = int(a_s), int(b_s)
            if a > b:
                raise ValueError(f"Invalid line range {a}-{b}")
            for x in range(a, b + 1):
                if x not in seen:
                    seen.add(x)
                    out.append(x)
        else:
            x = int(part)
            if x not in seen:
                seen.add(x)
                out.append(x)
    if not out:
        raise ValueError("Line list is empty.")
    return out


def validate_external_ranges(
    ranges: Sequence[Tuple[str, int, int]],
    external_dir: Path,
    expected_lines: Sequence[int],
) -> List[Tuple[str, int, int]]:
    """Strictly verify every prompt line appears once, with no gaps, overlaps, or missing WAVs."""
    expected_set = set(expected_lines)
    seen: Dict[int, str] = {}
    cleaned: List[Tuple[str, int, int]] = []
    for wav_name, lo, hi in ranges:
        if lo > hi:
            raise ValueError(f"Invalid line range {lo}..{hi} for {wav_name}")
        wav_path = external_dir / wav_name
        if not wav_path.is_file():
            raise FileNotFoundError(f"Missing external recording WAV: {wav_path}")
        for ln in range(lo, hi + 1):
            if ln in seen:
                raise ValueError(f"Overlap on prompt line {ln}: covered by both {seen[ln]} and {wav_name}")
            seen[ln] = wav_name
        cleaned.append((wav_name, lo, hi))

    missing = sorted(expected_set - set(seen))
    extra = sorted(set(seen) - expected_set)
    if missing:
        raise ValueError(f"Range map is missing {len(missing)} prompt line(s): {missing[:15]}")
    if extra:
        raise ValueError(f"Range map includes {len(extra)} out-of-range line(s): {extra[:15]}")
    return cleaned


def read_ranges_file(path: Path) -> List[Tuple[str, int, int]]:
    entries: List[Tuple[str, int, int]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 3:
            raise ValueError(f"Invalid range entry (expected 'WAV first last'): {line!r}")
        entries.append((parts[0], int(parts[1]), int(parts[2])))
    return entries


def write_ranges_file(path: Path, entries: Sequence[Tuple[str, int, int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# WAV_filename  first_line  last_line"]
    for wav_name, lo, hi in entries:
        lines.append(f"{wav_name} {lo} {hi}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def compute_frame_db(mono: np.ndarray, sr: int) -> Tuple[np.ndarray, float]:
    hop = max(1, int(sr * 0.010))
    win = max(hop, int(sr * 0.020))
    n_frames = max(1, (len(mono) - win) // hop + 1)
    cs = np.concatenate(([0.0], np.cumsum(mono.astype(np.float64) ** 2)))
    idx = np.arange(n_frames) * hop
    rms = np.sqrt(np.maximum(0.0, (cs[idx + win] - cs[idx]) / win))
    fdb = 20.0 * np.log10(np.maximum(rms, 1e-12))
    return fdb, float(np.percentile(fdb, 10))


def _token_for_line(item: Dict[str, object]) -> str:
    ln = int(item["line"])
    kind = str(item.get("kind", "word"))
    if kind == "word":
        # Keep token safe for whitespace-delimited MFA .lab files
        safe = re.sub(r"\s+", "_", str(item["text"]).strip())
        return f"l{ln:04d}_{safe}"
    return f"l{ln:04d}_sup"


def _build_mfa_dict_and_meta(
    project: VoiceProject,
    phone_map_res: PhoneMapResult,
    *,
    subset_lines: Optional[Sequence[int]] = None,
) -> Tuple[List[str], Dict[str, Dict[str, object]]]:
    inv = json.loads((project.plan_dir / "inventory.json").read_text(encoding="utf-8"))
    dmap = json.loads((project.plan_dir / "diphone_map.json").read_text(encoding="utf-8"))
    vowels = set(inv["vowels"])
    model_name = project.config.mfa_model
    phone_map = phone_map_res.mapping
    subset_set = set(subset_lines) if subset_lines is not None else None

    dict_lines: List[str] = []
    token_meta: Dict[str, Dict[str, object]] = {}

    for item in dmap["lines"]:
        ln = int(item["line"])
        if subset_set is not None and ln not in subset_set:
            continue
        tok = _token_for_line(item)
        phones = list(item["phones"])
        mfa_seq: List[str] = []
        spans: List[Tuple[str, int]] = []
        for idx, ph in enumerate(phones):
            seq = list(phone_map[ph])
            if model_name == "english_mfa":
                if idx == 0 and ph in ("p", "t", "k") and len(phones) > 1 and phones[1] in vowels:
                    seq = [ph + "ʰ"]
                if ph == "l" and (idx == len(phones) - 1 or phones[idx + 1] not in vowels):
                    seq = ["ɫ"]
            mfa_seq.extend(seq)
            spans.append((ph, len(seq)))

        dict_lines.append(f"{tok}\t{' '.join(mfa_seq)}")
        if model_name == "english_mfa":
            alt = [ENGLISH_MFA_VARIANTS.get(p, p) for p in mfa_seq]
            if alt != mfa_seq:
                dict_lines.append(f"{tok}\t{' '.join(alt)}")

        token_meta[tok] = {
            "line": ln,
            "kind": str(item.get("kind", "word")),
            "text": str(item["text"]),
            "phones": phones,
            "spans": spans,
        }

    return dict_lines, token_meta


_RE_PCT = re.compile(r"\b(\d{1,3})%")

_MFA_STAGE_MILESTONES: Sequence[Tuple[Tuple[str, ...], float]] = (
    (("setting up corpus", "creating corpus", "initializing"), 0.06),
    (("loading", "dictionary", "acoustic model"), 0.12),
    (("generating mfcc", "extracting feature", "mfcc"), 0.22),
    (("calculating cmvn", "cmvn"), 0.32),
    (("calculating fmllr", "fmllr"), 0.44),
    (("compiling", "training graph"), 0.54),
    (("generating alignment", "aligning"), 0.64),
    (("analyzing alignment", "collecting"), 0.80),
    (("exporting", "textgrid"), 0.90),
    (("done!", "finished"), 0.98),
)


def _estimate_mfa_subprogress(line: str, current: float, stage_floor: float) -> Tuple[float, float]:
    """Map MFA stdout stage text & optional tqdm/rich `%` into a monotonic 0.0..1.0 fraction."""
    low = line.lower()
    for keys, val in _MFA_STAGE_MILESTONES:
        if any(k in low for k in keys):
            if val > stage_floor:
                stage_floor = val
                current = max(current, val)
            break
    m = _RE_PCT.search(line)
    if m:
        pct = min(100, int(m.group(1)))
        # Advance smoothly across the current ~12% stage window
        window_hi = min(0.97, stage_floor + 0.12)
        est = stage_floor + (window_hi - stage_floor) * (pct / 100.0)
        current = max(current, est)
    return current, stage_floor


def _run_mfa_align(
    mfa_env_dir: Path,
    corpus_dir: Path,
    dict_path: Path,
    model_name: str,
    out_dir: Path,
    *,
    num_jobs: int = 1,
    beam: int = 100,
    retry_beam: int = 400,
    progress: Optional[ProgressFn] = None,
    cancel: Optional[CancelFn] = None,
    label: str = "MFA align",
    progress_start: float = 0.0,
    progress_end: float = 1.0,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(mfa_env_dir / "python.exe"),
        "-u",
        "-m",
        "montreal_forced_aligner.command_line.mfa",
        "align",
        str(corpus_dir),
        str(dict_path),
        model_name,
        str(out_dir),
        "--clean",
        "--single_speaker",
        "--num_jobs", str(num_jobs),
        "--beam", str(beam),
        "--retry_beam", str(retry_beam),
    ]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    env = mfa_subprocess_env(mfa_env_dir)
    env["PYTHONUNBUFFERED"] = "1"
    if progress:
        progress(f"{label}: launching...", progress_start)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        creationflags=creationflags,
    )
    tail: List[str] = []
    sub_frac = 0.02
    stage_floor = 0.02
    span = max(0.0, progress_end - progress_start)
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            if cancel and cancel():
                proc.terminate()
                raise CancelledError("Alignment cancelled by user.")
            s = line.strip()
            if s:
                tail.append(s)
                if len(tail) > 40:
                    tail.pop(0)
                sub_frac, stage_floor = _estimate_mfa_subprogress(s, sub_frac, stage_floor)
                if progress:
                    progress(f"{label}: {s[:110]}", progress_start + span * sub_frac)
        rc = proc.wait()
    except BaseException:
        proc.terminate()
        raise
    if rc != 0:
        raise RuntimeError(f"{label} failed (exit {rc}):\n" + "\n".join(tail[-20:]))
    if progress:
        progress(f"{label}: complete.", progress_end)


def _load_segments_csv(path: Path) -> Dict[int, Dict[str, str]]:
    if not path.is_file():
        return {}
    with path.open("r", encoding="utf-8", newline="") as f:
        return {int(r["line"]): dict(r) for r in csv.DictReader(f)}


def _save_segments_csv(path: Path, rows_by_line: Dict[int, Dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = [rows_by_line[k] for k in sorted(rows_by_line)]
    if not ordered:
        return
    fieldnames = [
        "line", "token", "kind", "text", "source_wav",
        "source_start_s", "source_end_s", "duration_s",
        "pass1_raw_dur_s", "trimmed_dur_s", "segment_wav",
    ]
    tmp = path.with_suffix(".csv.tmp")
    with tmp.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in ordered:
            w.writerow({k: r.get(k, "") for k in fieldnames})
    tmp.replace(path)


def audit_aligned_clips(
    project: VoiceProject,
    token_meta: Dict[str, Dict[str, object]],
) -> AlignmentReport:
    """Audit Pass-2 per-line TextGrids & clips to build the flagged-lines retake/pickup queue."""
    seg_rows = _load_segments_csv(project.segments_csv)
    by_line_meta = {int(v["line"]): (tok, v) for tok, v in token_meta.items()}
    flags: List[AuditFlag] = []
    aligned_ok = 0
    phone_ok = 0

    for ln in sorted(by_line_meta):
        tok, m = by_line_meta[ln]
        tg_path = project.mfa_aligned_dir / f"line_{ln:04d}.TextGrid"
        clip_path = project.segments_dir / f"line_{ln:04d}.wav"
        src_wav = seg_rows.get(ln, {}).get("source_wav", "")
        reasons: List[str] = []

        if not clip_path.is_file():
            flags.append(AuditFlag(ln, str(m["text"]), str(m["kind"]), ["MISSING_CLIP"], src_wav, ""))
            continue
        if not tg_path.is_file():
            flags.append(AuditFlag(ln, str(m["text"]), str(m["kind"]), ["UNALIGNED"], src_wav, str(clip_path)))
            continue

        tiers = parse_textgrid(tg_path)
        wivs = [w for w in tiers.get("words", []) if w["text"]]
        pivs = [p for p in tiers.get("phones", []) if p["text"] and p["text"] not in ("sil", "sp", "spn")]
        if not wivs:
            flags.append(AuditFlag(ln, str(m["text"]), str(m["kind"]), ["NO_WORD_ALIGNED"], src_wav, str(clip_path)))
            continue

        aligned_ok += 1
        w0, w1 = float(wivs[0]["xmin"]), float(wivs[0]["xmax"])
        w_phones = [
            p for p in pivs
            if w0 - 1e-4 <= 0.5 * (float(p["xmin"]) + float(p["xmax"])) <= w1 + 1e-4
        ]
        exp_n = sum(int(c) for _, c in m["spans"])
        if len(w_phones) == exp_n:
            phone_ok += 1
        else:
            reasons.append(f"PHONE_COUNT_MISMATCH({len(w_phones)}!={exp_n})")

        audio, sr = sf.read(str(clip_path), dtype="float32", always_2d=True)
        mono = audio[:, 0] if audio.shape[1] == 1 else audio.mean(axis=1)
        peak = float(np.max(np.abs(mono))) if len(mono) else 0.0
        if peak >= 0.99:
            reasons.append(f"CLIPPED(peak={peak:.2f})")

        frame_db, noise_p10 = compute_frame_db(mono, sr)
        i0, i1 = max(0, int(w0 * sr)), min(len(mono), int(w1 * sr))
        word_rms = 20.0 * math.log10(max(float(np.sqrt(np.mean(mono[i0:i1].astype(np.float64) ** 2))), 1e-12)) if i1 > i0 else -120.0
        if word_rms < noise_p10 + 10.0:
            reasons.append(f"LOW_SNR({word_rms - noise_p10:.1f}dB)")
        if (w1 - w0) < 0.12:
            reasons.append(f"TOO_SHORT({w1 - w0:.2f}s)")
        max_pd = max(((float(p["xmax"]) - float(p["xmin"])) * 1000.0 for p in w_phones), default=0.0)
        if max_pd > 550.0:
            reasons.append(f"LONG_PHONE_OR_SILENCE_LEAK({max_pd:.0f}ms)")

        if reasons:
            flags.append(AuditFlag(ln, str(m["text"]), str(m["kind"]), reasons, src_wav, str(clip_path)))

    audit_doc = {
        "total_lines": len(by_line_meta),
        "aligned_lines": aligned_ok,
        "phone_ok_lines": phone_ok,
        "flagged_count": len(flags),
        "flags": [
            {
                "line": f.line,
                "text": f.text,
                "kind": f.kind,
                "reasons": f.reasons,
                "source_wav": f.source_wav,
                "clip_wav": f.clip_wav,
            }
            for f in flags
        ],
    }
    project.audit_file.write_text(json.dumps(audit_doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return AlignmentReport(
        total_lines=len(by_line_meta),
        aligned_lines=aligned_ok,
        phone_ok_lines=phone_ok,
        flags=flags,
        phone_map_approximate=[],
    )


def _prepare_mfa_and_map(
    project: VoiceProject,
    *,
    progress: Optional[ProgressFn] = None,
    cancel: Optional[CancelFn] = None,
) -> Tuple[Path, PhoneMapResult]:
    def _scaled(msg: str, frac: Optional[float]) -> None:
        if progress:
            mapped = 0.02 + 0.06 * max(0.0, min(1.0, frac)) if frac is not None else 0.04
            progress(msg, mapped)

    mfa_env = ensure_mfa_env(progress=_scaled, cancel=cancel)
    model_name = project.config.mfa_model
    ensure_mfa_model(model_name, mfa_env_dir=mfa_env, progress=_scaled, cancel=cancel)
    model_phones = inspect_mfa_model_phones(model_name, mfa_env_dir=mfa_env, progress=_scaled, cancel=cancel)
    inv = json.loads((project.plan_dir / "inventory.json").read_text(encoding="utf-8"))
    pm = build_phone_map(inv, model_name, model_phones)
    if progress:
        progress(f"MFA model '{model_name}' and phone map ready.", 0.08)
    return mfa_env, pm


def find_unaligned_mic_lines(project: VoiceProject) -> List[int]:
    """Return planned line numbers that have a recorded mic take WAV but no TextGrid yet."""
    dmap_path = project.plan_dir / "diphone_map.json"
    if not dmap_path.is_file():
        return []
    dmap = json.loads(dmap_path.read_text(encoding="utf-8"))
    out: List[int] = []
    for it in dmap.get("lines", []):
        ln = int(it["line"])
        take_wav = project.takes_dir / f"line_{ln:04d}.wav"
        tg_path = project.mfa_aligned_dir / f"line_{ln:04d}.TextGrid"
        if take_wav.is_file() and take_wav.stat().st_size > 1000 and not tg_path.is_file():
            out.append(ln)
    return sorted(out)


def align_mic_takes(
    project: VoiceProject,
    *,
    only_lines: Optional[Sequence[int]] = None,
    num_jobs: int = 4,
    progress: Optional[ProgressFn] = None,
    cancel: Optional[CancelFn] = None,
) -> AlignmentReport:
    """Export built-in mic takes -> `align/segments/` and run Pass-2 per-line MFA directly."""
    mfa_env, pm = _prepare_mfa_and_map(project, progress=progress, cancel=cancel)
    dict_lines, full_meta = _build_mfa_dict_and_meta(project, pm)

    (project.align_dir / "mfa_dict.txt").write_text("\n".join(dict_lines) + "\n", encoding="utf-8")
    project.token_meta_file.write_text(json.dumps(full_meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    by_line = {int(v["line"]): (tok, v) for tok, v in full_meta.items()}
    target_lines = sorted(only_lines) if only_lines is not None else sorted(by_line)

    pass2_dir = project.align_dir / "mfa_pass2_corpus"
    if pass2_dir.exists():
        shutil.rmtree(pass2_dir)
    pass2_dir.mkdir(parents=True, exist_ok=True)
    project.segments_dir.mkdir(parents=True, exist_ok=True)

    rows_by_line = _load_segments_csv(project.segments_csv)
    missing_takes = []
    for ln in target_lines:
        take_wav = project.takes_dir / f"line_{ln:04d}.wav"
        if not take_wav.is_file():
            missing_takes.append(ln)
            continue
        tok, m = by_line[ln]
        info = sf.info(str(take_wav))
        dur = info.frames / float(info.samplerate)
        fname = f"line_{ln:04d}.wav"
        shutil.copy2(take_wav, project.segments_dir / fname)
        shutil.copy2(take_wav, pass2_dir / fname)
        (pass2_dir / f"line_{ln:04d}.lab").write_text(f"{tok}\n", encoding="utf-8")
        rows_by_line[ln] = {
            "line": ln,
            "token": tok,
            "kind": m["kind"],
            "text": m["text"],
            "source_wav": f"takes/{fname}",
            "source_start_s": "0.000000",
            "source_end_s": f"{dur:.6f}",
            "duration_s": f"{dur:.6f}",
            "pass1_raw_dur_s": f"{dur:.4f}",
            "trimmed_dur_s": f"{dur:.4f}",
            "segment_wav": fname,
        }

    if missing_takes:
        raise FileNotFoundError(f"Missing recorded takes for {len(missing_takes)} line(s): {missing_takes[:15]}")

    if progress:
        progress(f"Preparing {len(target_lines)} mic take(s) for Pass-2 MFA...", 0.14)
    _save_segments_csv(project.segments_csv, rows_by_line)

    out_tmp = project.align_dir / "mfa_aligned_run"
    if out_tmp.exists():
        shutil.rmtree(out_tmp)
    _run_mfa_align(
        mfa_env,
        pass2_dir,
        project.align_dir / "mfa_dict.txt",
        project.config.mfa_model,
        out_tmp,
        num_jobs=min(num_jobs, max(1, len(target_lines))),
        beam=40,
        retry_beam=160,
        progress=progress,
        cancel=cancel,
        label="Pass-2 MFA",
        progress_start=0.20,
        progress_end=0.92,
    )
    for tg in out_tmp.glob("*.TextGrid"):
        shutil.copy2(tg, project.mfa_aligned_dir / tg.name)
    shutil.rmtree(out_tmp, ignore_errors=True)

    if progress:
        progress("Auditing aligned clips...", 0.95)
    # Mark any pending extension/pickup batches whose lines now all have TextGrids as applied
    changed_batches = False
    for pb in project.config.pickups:
        if not pb.get("applied") and pb.get("lines"):
            if all((project.mfa_aligned_dir / f"line_{int(ln):04d}.TextGrid").is_file() for ln in pb["lines"]):
                pb["applied"] = True
                changed_batches = True
    if changed_batches:
        project.save()

    report = audit_aligned_clips(project, full_meta)
    report.phone_map_approximate = pm.approximate
    project.set_stage("align", "done" if not report.flags else "flagged")
    if progress:
        progress(f"Alignment finished: {report.aligned_lines}/{report.total_lines} lines aligned.", 1.0)
    return report


def _slice_from_whole_textgrid(
    src_wav_path: Path,
    rel_source_wav: str,
    tg_path: Path,
    token_meta: Dict[str, Dict[str, object]],
    segments_dir: Path,
    pass2_dir: Path,
    *,
    pad_s: float = 0.14,
) -> Dict[int, Dict[str, object]]:
    tiers = parse_textgrid(tg_path)
    word_ivs = [iv for iv in tiers.get("words", []) if iv["text"]]
    audio, sr = sf.read(str(src_wav_path), dtype="float32", always_2d=True)
    mono = audio[:, 0] if audio.shape[1] == 1 else audio.mean(axis=1)
    total_dur = len(mono) / float(sr)
    frame_db, noise_p10 = compute_frame_db(mono, sr)
    speech_thresh = noise_p10 + 12.0

    def trim_outer(t0: float, t1: float) -> Tuple[float, float]:
        f0 = max(0, int(t0 / 0.010))
        f1 = min(len(frame_db), int(math.ceil(t1 / 0.010)))
        if f1 <= f0:
            return t0, t1
        act = np.flatnonzero(frame_db[f0:f1] > speech_thresh)
        if len(act) == 0:
            return t0, t1
        a0 = (f0 + int(act[0])) * 0.010
        a1 = (f0 + int(act[-1])) * 0.010 + 0.020
        return max(t0, a0 - 0.040), min(t1, a1 + 0.040)

    trimmed = []
    for wiv in word_ivs:
        tok = str(wiv["text"])
        if tok not in token_meta:
            continue
        rs, re_ = float(wiv["xmin"]), float(wiv["xmax"])
        ts, te = trim_outer(rs, re_)
        trimmed.append((tok, rs, re_, ts, te))

    rows: Dict[int, Dict[str, object]] = {}
    for i, (tok, rs, re_, ts, te) in enumerate(trimmed):
        m = token_meta[tok]
        ln = int(m["line"])
        prev_end = trimmed[i - 1][4] if i > 0 else 0.0
        next_start = trimmed[i + 1][3] if i + 1 < len(trimmed) else total_dur
        clip_s = max(0.0, ts - pad_s, 0.5 * (prev_end + ts) if i > 0 else 0.0)
        clip_e = min(total_dur, te + pad_s, 0.5 * (te + next_start) if i + 1 < len(trimmed) else total_dur)

        s_idx = int(round(clip_s * sr))
        e_idx = int(round(clip_e * sr))
        clip = mono[s_idx:e_idx]
        fname = f"line_{ln:04d}.wav"
        sf.write(str(segments_dir / fname), clip, sr, subtype="PCM_16")
        sf.write(str(pass2_dir / fname), clip, sr, subtype="PCM_16")
        (pass2_dir / f"line_{ln:04d}.lab").write_text(f"{tok}\n", encoding="utf-8")

        rows[ln] = {
            "line": ln,
            "token": tok,
            "kind": m["kind"],
            "text": m["text"],
            "source_wav": rel_source_wav,
            "source_start_s": f"{s_idx / sr:.6f}",
            "source_end_s": f"{e_idx / sr:.6f}",
            "duration_s": f"{(e_idx - s_idx) / sr:.6f}",
            "pass1_raw_dur_s": f"{re_ - rs:.4f}",
            "trimmed_dur_s": f"{te - ts:.4f}",
            "segment_wav": fname,
        }
    return rows


def align_external_recordings(
    project: VoiceProject,
    *,
    num_jobs_pass2: int = 4,
    pad_s: float = 0.14,
    progress: Optional[ProgressFn] = None,
    cancel: Optional[CancelFn] = None,
) -> AlignmentReport:
    """Full Pass-1 -> slice -> Pass-2 MFA pipeline for external recorder long WAV(s)."""
    if not project.ranges_file.is_file():
        raise FileNotFoundError(f"Missing external range map: {project.ranges_file}")

    mfa_env, pm = _prepare_mfa_and_map(project, progress=progress, cancel=cancel)
    dict_lines, full_meta = _build_mfa_dict_and_meta(project, pm)
    by_line = {int(v["line"]): (tok, v) for tok, v in full_meta.items()}

    raw_ranges = read_ranges_file(project.ranges_file)
    ranges = validate_external_ranges(raw_ranges, project.external_dir, sorted(by_line))

    (project.align_dir / "mfa_dict.txt").write_text("\n".join(dict_lines) + "\n", encoding="utf-8")
    project.token_meta_file.write_text(json.dumps(full_meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    whole_dir = project.align_dir / "whole_corpus"
    if whole_dir.exists():
        shutil.rmtree(whole_dir)
    whole_dir.mkdir(parents=True, exist_ok=True)

    for r_i, (wav_name, lo, hi) in enumerate(ranges):
        src_path = project.external_dir / wav_name
        stem = Path(wav_name).stem
        if progress:
            frac = 0.08 + 0.06 * (r_i / float(max(1, len(ranges))))
            progress(f"Resampling {wav_name} to 16 kHz mono for Pass-1 MFA...", frac)
        audio, sr = sf.read(str(src_path), dtype="float32", always_2d=True)
        mono = audio[:, 0] if audio.shape[1] == 1 else audio.mean(axis=1)
        g = math.gcd(16000, sr)
        mono16 = resample_poly(mono, 16000 // g, sr // g).astype(np.float32) if sr != 16000 else mono
        sf.write(str(whole_dir / f"{stem}.wav"), mono16, 16000, subtype="PCM_16")
        toks = [by_line[ln][0] for ln in range(lo, hi + 1)]
        (whole_dir / f"{stem}.lab").write_text(" ".join(toks) + "\n", encoding="utf-8")

    aligned_whole = project.align_dir / "aligned_whole"
    _run_mfa_align(
        mfa_env,
        whole_dir,
        project.align_dir / "mfa_dict.txt",
        project.config.mfa_model,
        aligned_whole,
        num_jobs=1,
        beam=100,
        retry_beam=400,
        progress=progress,
        cancel=cancel,
        label="Pass-1 MFA",
        progress_start=0.14,
        progress_end=0.50,
    )

    pass2_dir = project.align_dir / "mfa_pass2_corpus"
    if pass2_dir.exists():
        shutil.rmtree(pass2_dir)
    pass2_dir.mkdir(parents=True, exist_ok=True)
    project.segments_dir.mkdir(parents=True, exist_ok=True)

    rows_by_line: Dict[int, Dict[str, object]] = {}
    for r_i, (wav_name, lo, hi) in enumerate(ranges):
        stem = Path(wav_name).stem
        tg_path = aligned_whole / f"{stem}.TextGrid"
        if not tg_path.is_file():
            raise RuntimeError(f"Pass-1 MFA did not produce TextGrid for {wav_name}")
        if progress:
            progress(f"Slicing {wav_name} into per-line clips...", 0.51 + 0.06 * (r_i / float(max(1, len(ranges)))))
        sliced = _slice_from_whole_textgrid(
            project.external_dir / wav_name,
            f"external/{wav_name}",
            tg_path,
            full_meta,
            project.segments_dir,
            pass2_dir,
            pad_s=pad_s,
        )
        rows_by_line.update(sliced)

    _save_segments_csv(project.segments_csv, rows_by_line)

    _run_mfa_align(
        mfa_env,
        pass2_dir,
        project.align_dir / "mfa_dict.txt",
        project.config.mfa_model,
        project.mfa_aligned_dir,
        num_jobs=num_jobs_pass2,
        beam=40,
        retry_beam=160,
        progress=progress,
        cancel=cancel,
        label="Pass-2 MFA",
        progress_start=0.58,
        progress_end=0.94,
    )

    if progress:
        progress("Auditing aligned clips...", 0.96)
    report = audit_aligned_clips(project, full_meta)
    report.phone_map_approximate = pm.approximate
    project.set_stage("align", "done" if not report.flags else "flagged")
    if progress:
        progress(f"Alignment finished: {report.aligned_lines}/{report.total_lines} lines aligned.", 1.0)
    return report


def export_pickup_script(project: VoiceProject, lines: Sequence[int]) -> Tuple[str, Path, Path]:
    """Export a compact recording script for ONLY the flagged/selected line numbers."""
    dmap = json.loads((project.plan_dir / "diphone_map.json").read_text(encoding="utf-8"))
    by_line = {int(x["line"]): x for x in dmap["lines"]}
    ordered = [int(ln) for ln in lines]
    missing = [ln for ln in ordered if ln not in by_line]
    if missing:
        raise ValueError(f"Unknown line number(s) in pickup list: {missing}")

    project.pickups_dir.mkdir(parents=True, exist_ok=True)
    batch_num = len(project.config.pickups) + 1
    batch_id = f"pickup_{batch_num:02d}"

    script_lines = [
        f"# DiphoneForge Pickup Script — {batch_id} ({len(ordered)} line(s))",
        "# Record these lines in order, leaving a brief pause (~0.5 s) between each line.",
        "",
    ]
    for idx, ln in enumerate(ordered, 1):
        item = by_line[ln]
        txt = item["text"] if item.get("kind") == "word" else f"[{item['text']}]"
        script_lines.append(f"{idx:03d}  (Line {ln:04d})  {txt}")

    script_path = project.pickups_dir / f"{batch_id}_script.txt"
    meta_path = project.pickups_dir / f"{batch_id}_meta.json"
    script_path.write_text("\n".join(script_lines) + "\n", encoding="utf-8")
    meta_doc = {"id": batch_id, "lines": ordered, "applied": False}
    meta_path.write_text(json.dumps(meta_doc, indent=2) + "\n", encoding="utf-8")
    project.config.pickups.append(meta_doc)
    project.save()
    return batch_id, script_path, meta_path


def apply_pickup_recording(
    project: VoiceProject,
    pickup_wav: Path,
    lines: Sequence[int],
    *,
    batch_id: Optional[str] = None,
    pad_s: float = 0.14,
    progress: Optional[ProgressFn] = None,
    cancel: Optional[CancelFn] = None,
) -> AlignmentReport:
    """Align & slice a short external pickup WAV covering ONLY `lines` and patch ONLY those lines!

    Never touches the original full recordings or any other line in `segments.csv` / `mfa_aligned/`.
    """
    if not pickup_wav.is_file():
        raise FileNotFoundError(f"Pickup WAV not found: {pickup_wav}")
    ordered = [int(ln) for ln in lines]
    if not ordered:
        raise ValueError("Pickup line list cannot be empty.")

    mfa_env, pm = _prepare_mfa_and_map(project, progress=progress, cancel=cancel)
    dict_lines, full_meta = _build_mfa_dict_and_meta(project, pm)
    by_line = {int(v["line"]): (tok, v) for tok, v in full_meta.items()}

    # Ensure read-only copy lives inside recordings/pickups/ so relative source_wav is portable
    project.pickups_dir.mkdir(parents=True, exist_ok=True)
    matched_batch = None
    if batch_id is None:
        for pb in reversed(project.config.pickups):
            if not pb.get("applied") and [int(x) for x in pb.get("lines", [])] == ordered:
                matched_batch = pb
                break
    tag = batch_id or (str(matched_batch["id"]) if matched_batch is not None else f"pickup_{len(project.config.pickups) + 1:02d}")
    dest_wav = project.pickups_dir / f"{tag}_{pickup_wav.name}"
    if pickup_wav.resolve() != dest_wav.resolve():
        shutil.copy2(pickup_wav, dest_wav)

    work = project.align_dir / "pickup_work" / tag
    if work.exists():
        shutil.rmtree(work)
    whole_dir = work / "whole_corpus"
    aligned_whole = work / "aligned_whole"
    pass2_dir = work / "mfa_pass2_corpus"
    aligned_p2 = work / "mfa_aligned"
    for d in (whole_dir, aligned_whole, pass2_dir, aligned_p2):
        d.mkdir(parents=True, exist_ok=True)

    dict_path = work / "mfa_dict.txt"
    dict_path.write_text("\n".join(dict_lines) + "\n", encoding="utf-8")

    # Resample pickup WAV for Pass-1 MFA
    if progress:
        progress(f"Preparing pickup/extension recording ({len(ordered)} line(s))...", 0.10)
    audio, sr = sf.read(str(dest_wav), dtype="float32", always_2d=True)
    mono = audio[:, 0] if audio.shape[1] == 1 else audio.mean(axis=1)
    g = math.gcd(16000, sr)
    mono16 = resample_poly(mono, 16000 // g, sr // g).astype(np.float32) if sr != 16000 else mono
    sf.write(str(whole_dir / f"{tag}.wav"), mono16, 16000, subtype="PCM_16")
    toks = [by_line[ln][0] for ln in ordered]
    (whole_dir / f"{tag}.lab").write_text(" ".join(toks) + "\n", encoding="utf-8")

    _run_mfa_align(
        mfa_env,
        whole_dir,
        dict_path,
        project.config.mfa_model,
        aligned_whole,
        num_jobs=1,
        beam=100,
        retry_beam=400,
        progress=progress,
        cancel=cancel,
        label=f"Pickup {tag} Pass-1",
        progress_start=0.14,
        progress_end=0.50,
    )

    tg_whole = aligned_whole / f"{tag}.TextGrid"
    if not tg_whole.is_file():
        raise RuntimeError(f"Pickup Pass-1 MFA failed to align {dest_wav.name}")

    if progress:
        progress(f"Slicing {len(ordered)} pickup/extension line(s)...", 0.54)
    patched_rows = _slice_from_whole_textgrid(
        dest_wav,
        f"pickups/{dest_wav.name}",
        tg_whole,
        full_meta,
        project.segments_dir,
        pass2_dir,
        pad_s=pad_s,
    )
    if not patched_rows:
        raise RuntimeError(f"Pickup alignment yielded 0 aligned words from {dest_wav.name}")

    _run_mfa_align(
        mfa_env,
        pass2_dir,
        dict_path,
        project.config.mfa_model,
        aligned_p2,
        num_jobs=1,
        beam=40,
        retry_beam=160,
        progress=progress,
        cancel=cancel,
        label=f"Pickup {tag} Pass-2",
        progress_start=0.58,
        progress_end=0.92,
    )

    # Patch ONLY the updated lines into segments.csv and mfa_aligned/
    if progress:
        progress("Patching segments, alignments, and token metadata...", 0.94)
    existing_rows = _load_segments_csv(project.segments_csv)
    existing_rows.update(patched_rows)
    _save_segments_csv(project.segments_csv, existing_rows)
    for ln in patched_rows:
        src_tg = aligned_p2 / f"line_{ln:04d}.TextGrid"
        if src_tg.is_file():
            shutil.copy2(src_tg, project.mfa_aligned_dir / src_tg.name)

    # Merge full token_meta.json so newly extended/patched lines are always registered while preserving existing tokens
    merged_meta: Dict[str, Dict[str, object]] = {}
    if project.token_meta_file.is_file():
        try:
            merged_meta = json.loads(project.token_meta_file.read_text(encoding="utf-8"))
        except Exception:
            merged_meta = {}
    existing_lines_in_meta = {int(v["line"]) for v in merged_meta.values()}
    ordered_set = set(ordered)
    for tok, meta_entry in full_meta.items():
        ln = int(meta_entry["line"])
        if ln in ordered_set or ln not in existing_lines_in_meta:
            merged_meta[tok] = meta_entry
    tmp_meta = project.token_meta_file.with_suffix(".json.tmp")
    tmp_meta.write_text(json.dumps(merged_meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp_meta.replace(project.token_meta_file)
    (project.align_dir / "mfa_dict.txt").write_text("\n".join(dict_lines) + "\n", encoding="utf-8")

    # Mark pickup/extension batch applied in project.json
    updated_existing = False
    for pb in project.config.pickups:
        if pb.get("id") == tag:
            pb["applied"] = True
            pb["wav"] = f"pickups/{dest_wav.name}"
            updated_existing = True
            break
    if not updated_existing:
        project.config.pickups.append({"id": tag, "lines": ordered, "wav": f"pickups/{dest_wav.name}", "applied": True})
    project.save()

    report = audit_aligned_clips(project, merged_meta)
    report.phone_map_approximate = pm.approximate
    project.set_stage("align", "done" if not report.flags else "flagged")
    if progress:
        progress(f"Patched {len(patched_rows)} line(s). Total aligned: {report.aligned_lines}/{report.total_lines}.", 1.0)
    return report
