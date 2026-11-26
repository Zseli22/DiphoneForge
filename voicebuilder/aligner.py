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
from dataclasses import dataclass, field
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
    warnings: List[str] = field(default_factory=list)


@dataclass
class ExternalWavInspection:
    wav_name: str
    duration_s: float
    expected_count: int
    detected_count: int
    delta: int
    utterances: List[Tuple[float, float, float]]  # (t0_s, t1_s, top6_db)
    loose_utterances: List[Tuple[float, float, float]]
    noise_p10_db: float
    speech_ref_db: float
    direct_vad_ok: bool
    shift_warning: str = ""
    suspected_line: Optional[int] = None
    suspected_text: str = ""
    suspected_time_s: Optional[float] = None
    status_label: str = ""


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


def detect_spoken_utterances(
    mono: np.ndarray,
    sr: int,
) -> Tuple[List[Tuple[float, float, float]], List[Tuple[float, float, float]], float, float]:
    """Detect isolated spoken utterances in a recording via hysteresis energy VAD.

    Returns `(strict_utts, loose_utts, noise_p10_db, speech_ref_db)` where each
    utterance is `(start_s, end_s, top6_db)`.
    - `strict_utts` rejects soft breaths, faint headphone bleed, and sharp recorder
      button clicks so utterance counts match true spoken words cleanly.
    - `loose_utts` keeps slightly quieter bursts (down to -9.5 dB below median) so
      Pass-1 fallback never discards a genuinely quiet spoken word.
    """
    total_dur = len(mono) / float(sr) if sr > 0 else 0.0
    if len(mono) < max(1, int(sr * 0.06)):
        return [], [], -120.0, -120.0

    fdb, noise_p10 = compute_frame_db(mono, sr)
    above_noise = fdb[fdb > noise_p10 + 12.0]
    speech_ref = (
        float(np.percentile(above_noise, 85))
        if len(above_noise) >= 10
        else float(np.percentile(fdb, 95))
    )
    if speech_ref < noise_p10 + 8.0:
        return [], [], noise_p10, speech_ref

    core_thresh = max(noise_p10 + 14.0, speech_ref - 12.0)
    edge_thresh = max(noise_p10 + 8.0, speech_ref - 26.0)

    # 1. Raw core runs (> core_thresh), dropping isolated <=2-frame (20 ms) impulses before merging
    active = fdb > core_thresh
    raw: List[List[int]] = []
    i = 0
    n_f = len(active)
    while i < n_f:
        if active[i]:
            j = i
            while j < n_f and active[j]:
                j += 1
            if (j - i) >= 3:
                raw.append([i, j, j - i])
            i = j
        else:
            i += 1

    # 2. Merge word-internal stop closures / geminates separated by <= 0.38 s (38 frames)
    merged: List[List[int]] = []
    for s, e, cnt in raw:
        if merged and (s - merged[-1][1]) <= 38:
            merged[-1][1] = e
            merged[-1][2] += cnt
        else:
            merged.append([s, e, cnt])

    # 3. Measure sustained top-6-frame speech energy and filter sharp mechanical clicks
    cands: List[Tuple[int, int, int, float, float]] = []
    for s, e, cnt in merged:
        if cnt < 5:
            continue
        vals = np.sort(fdb[s:e])
        top6 = float(np.mean(vals[-min(6, len(vals)):]))
        pk = float(vals[-1])
        # Sharp mechanical button/handling clicks have a brief spike with little sustained energy
        if cnt < 10 and (pk - top6) > 5.0:
            continue
        cands.append((s, e, cnt, top6, pk))

    if not cands:
        return [], [], noise_p10, speech_ref

    med_top6 = float(np.median([c[3] for c in cands]))

    def _is_boundary_click(s: int, e: int, cnt: int, top6: float) -> bool:
        t0 = s * 0.010
        t1 = e * 0.010 + 0.020
        at_edge = (t0 < 0.35) or (t1 > total_dur - 0.35)
        return at_edge and cnt < 10 and top6 < (med_top6 - 3.0)

    strict_raw = [c for c in cands if c[3] >= med_top6 - 6.5 and not _is_boundary_click(c[0], c[1], c[2], c[3])]
    loose_raw = [c for c in cands if c[3] >= med_top6 - 9.5 and not _is_boundary_click(c[0], c[1], c[2], c[3])]

    def _expand_edges(items: List[Tuple[int, int, int, float, float]]) -> List[Tuple[float, float, float]]:
        out: List[Tuple[float, float, float]] = []
        for k, (s, e, _cnt, top6, _pk) in enumerate(items):
            left_lim = (items[k - 1][1] + s) // 2 if k > 0 else 0
            right_lim = (e + items[k + 1][0]) // 2 if k + 1 < len(items) else n_f
            es = s
            sil = 0
            while es > left_lim:
                if fdb[es - 1] > edge_thresh:
                    es -= 1
                    sil = 0
                else:
                    sil += 1
                    if sil >= 4:
                        break
                    es -= 1
            while es < s and fdb[es] <= edge_thresh:
                es += 1

            ee = e
            sil = 0
            while ee < right_lim:
                if fdb[ee] > edge_thresh:
                    ee += 1
                    sil = 0
                else:
                    sil += 1
                    if sil >= 4:
                        break
                    ee += 1
            while ee > e and fdb[ee - 1] <= edge_thresh:
                ee -= 1
            out.append((es * 0.010, min(total_dur, ee * 0.010 + 0.020), top6))
        return out

    return _expand_edges(strict_raw), _expand_edges(loose_raw), noise_p10, speech_ref


def _safe_corr(a: np.ndarray, b: np.ndarray) -> float:
    if len(a) < 6 or len(a) != len(b):
        return 0.0
    sa = float(np.std(a))
    sb = float(np.std(b))
    if sa < 1e-6 or sb < 1e-6:
        return 0.0
    return float(np.corrcoef(a, b)[0, 1])


def _analyze_utterance_shift(
    utts: Sequence[Tuple[float, float, float]],
    line_nos: Sequence[int],
    line_phone_counts: Dict[int, int],
    line_texts: Optional[Dict[int, str]] = None,
) -> Tuple[str, Optional[int], str, Optional[float]]:
    """Detect whether utterance durations shift out of sync with prompt phone counts.

    Returns `(warning_text, suspected_line, suspected_text, suspected_time_s)`.
    """
    m = len(utts)
    n = len(line_nos)
    if m < 10 or n < 10:
        return "", None, "", None

    durs = np.array([e - s for s, e, _ in utts], dtype=np.float64)
    nph = np.array([max(1, int(line_phone_counts.get(ln, 4))) for ln in line_nos], dtype=np.float64)
    med_rate = float(np.median(durs) / max(1.0, float(np.median(nph))))
    exp_durs = 0.12 + 0.85 * med_rate * nph
    exp_durs *= float(np.median(durs) / max(1e-6, float(np.median(exp_durs))))

    # Fast exact 1-event skip/extra locator when |m - n| == 1
    if abs(m - n) == 1:
        k_len = min(m, n)
        if m == n - 1:
            # 1 skipped prompt line at index k (0..n-1): utts[:k] <-> lines[:k], utts[k:] <-> lines[k+1:]
            err_pref = np.abs(durs - exp_durs[:-1])
            err_suff = np.abs(durs - exp_durs[1:])
            cs_pref = np.concatenate(([0.0], np.cumsum(err_pref)))
            cs_suff = np.concatenate((np.cumsum(err_suff[::-1])[::-1], [0.0]))
            tot = cs_pref + cs_suff  # length n (k = 0..n-1)
            best_k = int(np.argmin(tot))
            ln = int(line_nos[best_k])
            txt = (line_texts or {}).get(ln, "")
            t_s = utts[min(best_k, m - 1)][0]
            txt_part = f" ('{txt}')" if txt else ""
            return (
                f"Likely 1 skipped word near line {ln}{txt_part} (~{t_s:.1f} s).",
                ln,
                txt,
                t_s,
            )
        else:
            # 1 extra utterance at index k (0..m-1): utts[:k] <-> lines[:k], utts[k+1:] <-> lines[k:]
            err_pref = np.abs(durs[:-1] - exp_durs)
            err_suff = np.abs(durs[1:] - exp_durs)
            cs_pref = np.concatenate(([0.0], np.cumsum(err_pref)))
            cs_suff = np.concatenate((np.cumsum(err_suff[::-1])[::-1], [0.0]))
            tot = cs_pref + cs_suff  # length m (k = 0..m-1)
            best_k = int(np.argmin(tot))
            ln = int(line_nos[min(best_k, n - 1)])
            txt = (line_texts or {}).get(ln, "")
            t_s = utts[best_k][0]
            txt_part = f" (near '{txt}')" if txt else ""
            return (
                f"Likely 1 extra sound/retake around {t_s:.1f} s near line {ln}{txt_part}.",
                ln,
                txt,
                t_s,
            )

    # Rolling window correlation check (catches cancelling skip+noise when m == n, or locates onset when |m - n| >= 2)
    win = min(32, max(12, n // 3))
    step = max(6, win // 3)
    max_sh = max(2, min(8, abs(m - n) + 2))
    limit = min(m, n) - win
    for start in range(0, max(1, limit + 1), step):
        end = min(min(m, n), start + win)
        if end - start < 10:
            continue
        r0 = _safe_corr(durs[start:end], nph[start:end])
        best_sh = 0
        best_r = r0
        for sh in range(-max_sh, max_sh + 1):
            if sh == 0:
                continue
            b_s, b_e = start + sh, end + sh
            if b_s < 0 or b_e > n:
                continue
            r = _safe_corr(durs[start:end], nph[b_s:b_e])
            if r > best_r:
                best_r = r
                best_sh = sh
        if best_sh != 0 and best_r >= 0.45 and (best_r - r0) >= 0.30 and r0 < 0.30:
            ln = int(line_nos[start])
            txt = (line_texts or {}).get(ln, "")
            t_s = utts[start][0]
            kind_note = (
                "Possible cancelling skip + extra noise"
                if m == n
                else f"Alignment shift ({best_sh:+d} lines) detected"
            )
            return (
                f"{kind_note} around line {ln} (~{t_s:.1f} s).",
                ln,
                txt,
                t_s,
            )

    return "", None, "", None


def inspect_external_wav(
    wav_path: Path,
    expected_lines: Sequence[int],
    line_phone_counts: Optional[Dict[int, int]] = None,
    line_texts: Optional[Dict[int, str]] = None,
) -> ExternalWavInspection:
    """Fast pre-flight inspection of an external WAV against its expected prompt line list."""
    audio, sr = sf.read(str(wav_path), dtype="float32", always_2d=True)
    mono = audio[:, 0] if audio.shape[1] == 1 else audio.mean(axis=1)
    dur_s = len(mono) / float(sr) if sr > 0 else 0.0
    strict_utts, loose_utts, noise_p10, speech_ref = detect_spoken_utterances(mono, sr)
    exp_n = len(expected_lines)
    det_n = len(strict_utts)
    delta = det_n - exp_n

    shift_warn = ""
    susp_line: Optional[int] = None
    susp_text = ""
    susp_time: Optional[float] = None
    if line_phone_counts:
        shift_warn, susp_line, susp_text, susp_time = _analyze_utterance_shift(
            strict_utts, expected_lines, line_phone_counts, line_texts
        )

    direct_ok = (delta == 0) and (exp_n > 0) and (not shift_warn)

    if direct_ok:
        status_label = f"{det_n} / {exp_n} (Exact match)"
    elif delta == 0 and shift_warn:
        status_label = f"{det_n} / {exp_n} (Shift warning — line ~{susp_line})"
    elif delta < 0:
        hint = f", skip near line {susp_line}" if susp_line is not None else ""
        status_label = f"{det_n} / {exp_n} ({delta:+d} fewer{hint})"
    else:
        hint = f", extra near line {susp_line}" if susp_line is not None else ""
        status_label = f"{det_n} / {exp_n} ({delta:+d} extra{hint})"

    return ExternalWavInspection(
        wav_name=wav_path.name,
        duration_s=dur_s,
        expected_count=exp_n,
        detected_count=det_n,
        delta=delta,
        utterances=strict_utts,
        loose_utterances=loose_utts,
        noise_p10_db=noise_p10,
        speech_ref_db=speech_ref,
        direct_vad_ok=direct_ok,
        shift_warning=shift_warn,
        suspected_line=susp_line,
        suspected_text=susp_text,
        suspected_time_s=susp_time,
        status_label=status_label,
    )


def inspect_external_ranges(
    project: VoiceProject,
    ranges: Sequence[Tuple[str, int, int]],
) -> List[ExternalWavInspection]:
    """Run fast spoken-utterance inspection on every external WAV in `ranges`."""
    dmap_path = project.plan_dir / "diphone_map.json"
    phone_counts: Dict[int, int] = {}
    line_texts: Dict[int, str] = {}
    if dmap_path.is_file():
        dmap = json.loads(dmap_path.read_text(encoding="utf-8"))
        for it in dmap.get("lines", []):
            ln = int(it["line"])
            phone_counts[ln] = len(it.get("phones", []))
            line_texts[ln] = str(it.get("text", ""))
    out: List[ExternalWavInspection] = []
    for wav_name, lo, hi in ranges:
        wav_path = project.external_dir / wav_name
        if not wav_path.is_file() or lo > hi:
            continue
        out.append(
            inspect_external_wav(
                wav_path,
                list(range(lo, hi + 1)),
                line_phone_counts=phone_counts,
                line_texts=line_texts,
            )
        )
    return out


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
    *,
    warnings: Optional[Sequence[str]] = None,
) -> AlignmentReport:
    """Audit Pass-2 per-line TextGrids & clips to build the flagged-lines retake/pickup queue."""
    seg_rows = _load_segments_csv(project.segments_csv)
    by_line_meta = {int(v["line"]): (tok, v) for tok, v in token_meta.items()}
    dismissed_set = {int(x) for x in getattr(project.config, "dismissed_flags", [])}
    excluded_set = {int(x) for x in getattr(project.config, "excluded_lines", [])}
    flags: List[AuditFlag] = []
    all_raw_flagged_lines: List[int] = []
    aligned_ok = 0
    phone_ok = 0
    warn_list = list(warnings) if warnings is not None else []
    if warnings is None and project.audit_file.is_file():
        try:
            prev_doc = json.loads(project.audit_file.read_text(encoding="utf-8"))
            warn_list = list(prev_doc.get("warnings", []))
        except Exception:
            warn_list = []

    for ln in sorted(by_line_meta):
        tok, m = by_line_meta[ln]
        tg_path = project.mfa_aligned_dir / f"line_{ln:04d}.TextGrid"
        clip_path = project.segments_dir / f"line_{ln:04d}.wav"
        src_wav = seg_rows.get(ln, {}).get("source_wav", "")
        reasons: List[str] = []

        if ln in excluded_set:
            continue

        if not clip_path.is_file():
            all_raw_flagged_lines.append(ln)
            if ln not in dismissed_set:
                flags.append(AuditFlag(ln, str(m["text"]), str(m["kind"]), ["MISSING_CLIP"], src_wav, ""))
            continue
        if not tg_path.is_file():
            all_raw_flagged_lines.append(ln)
            if ln not in dismissed_set:
                flags.append(AuditFlag(ln, str(m["text"]), str(m["kind"]), ["UNALIGNED"], src_wav, str(clip_path)))
            continue

        tiers = parse_textgrid(tg_path)
        wivs = [w for w in tiers.get("words", []) if w["text"]]
        pivs = [p for p in tiers.get("phones", []) if p["text"] and p["text"] not in ("sil", "sp", "spn")]
        if not wivs:
            all_raw_flagged_lines.append(ln)
            if ln not in dismissed_set:
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
        if peak >= 0.995:
            reasons.append(f"CLIPPED(peak={peak:.2f})")

        frame_db, noise_p10 = compute_frame_db(mono, sr)
        i0, i1 = max(0, int(w0 * sr)), min(len(mono), int(w1 * sr))
        word_rms = 20.0 * math.log10(max(float(np.sqrt(np.mean(mono[i0:i1].astype(np.float64) ** 2))), 1e-12)) if i1 > i0 else -120.0
        if word_rms < noise_p10 + 10.0:
            reasons.append(f"LOW_SNR({word_rms - noise_p10:.1f}dB)")
        if (w1 - w0) < 0.10:
            reasons.append(f"TOO_SHORT({w1 - w0:.2f}s)")
        max_pd = max(((float(p["xmax"]) - float(p["xmin"])) * 1000.0 for p in w_phones), default=0.0)
        if max_pd > 550.0:
            reasons.append(f"LONG_PHONE_OR_SILENCE_LEAK({max_pd:.0f}ms)")

        if reasons:
            all_raw_flagged_lines.append(ln)
            if ln not in dismissed_set:
                flags.append(AuditFlag(ln, str(m["text"]), str(m["kind"]), reasons, src_wav, str(clip_path)))

    audit_doc = {
        "total_lines": len(by_line_meta),
        "aligned_lines": aligned_ok,
        "phone_ok_lines": phone_ok,
        "flagged_count": len(flags),
        "dismissed_lines": sorted(dismissed_set),
        "excluded_lines": sorted(excluded_set),
        "raw_flagged_lines": sorted(all_raw_flagged_lines),
        "warnings": warn_list,
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
        warnings=warn_list,
    )


def re_audit_project(project: VoiceProject) -> Optional[AlignmentReport]:
    """Recompute `audit.json` immediately after dismissing flags or excluding lines (no MFA rerun)."""
    if not project.token_meta_file.is_file():
        return None
    full_meta = json.loads(project.token_meta_file.read_text(encoding="utf-8"))
    rep = audit_aligned_clips(project, full_meta)
    project.set_stage("align", "done" if not rep.flags else "flagged")
    return rep


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
    """Return planned line numbers that have a new or re-recorded mic take WAV needing alignment."""
    dmap_path = project.plan_dir / "diphone_map.json"
    if not dmap_path.is_file():
        return []
    dmap = json.loads(dmap_path.read_text(encoding="utf-8"))
    out: List[int] = []
    for it in dmap.get("lines", []):
        ln = int(it["line"])
        take_wav = project.takes_dir / f"line_{ln:04d}.wav"
        tg_path = project.mfa_aligned_dir / f"line_{ln:04d}.TextGrid"
        if take_wav.is_file() and take_wav.stat().st_size > 1000:
            if (not tg_path.is_file()) or (take_wav.stat().st_mtime > tg_path.stat().st_mtime + 0.5):
                out.append(ln)
    return sorted(out)


def _trim_mic_take_silence(
    mono: np.ndarray,
    sr: int,
    *,
    pad_s: float = 0.18,
) -> Tuple[int, int]:
    """Return `(start_sample, end_sample)` trimming excess outer silence while keeping `pad_s` margin.

    Requires sustained speech (>= 3-4 consecutive active 10 ms frames) so isolated keyboard/mouse
    clicks or breath pops before the word cannot prevent leading silence from being cropped.
    """
    n = len(mono)
    if n < int(sr * 0.40):
        return 0, n
    frame_db, noise_p10 = compute_frame_db(mono, sr)
    thresh = max(noise_p10 + 10.0, -48.0)
    mask = (frame_db > thresh).astype(np.int32)
    if len(mask) >= 4:
        runs = np.convolve(mask, np.ones(4, dtype=np.int32), mode="same")
        sustained = np.flatnonzero((mask > 0) & (runs >= 3))
    else:
        sustained = np.flatnonzero(mask > 0)
    act = sustained if len(sustained) > 0 else np.flatnonzero(mask > 0)
    if len(act) == 0:
        return 0, n
    a0 = int(act[0]) * 0.010
    a1 = int(act[-1]) * 0.010 + 0.020
    t0 = max(0.0, a0 - pad_s)
    t1 = min(n / float(sr), a1 + pad_s)
    s0 = max(0, int(round(t0 * sr)))
    s1 = min(n, int(round(t1 * sr)))
    if (s1 - s0) < int(sr * 0.25):
        return 0, n
    return s0, s1


def align_mic_takes(
    project: VoiceProject,
    *,
    only_lines: Optional[Sequence[int]] = None,
    num_jobs: int = 4,
    progress: Optional[ProgressFn] = None,
    cancel: Optional[CancelFn] = None,
) -> AlignmentReport:
    """Export built-in mic takes (trimmed cleanly with exact source_start_s mapping) -> Pass-2 MFA."""
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
    prepared_count = 0
    missing_takes = []
    for ln in target_lines:
        take_wav = project.takes_dir / f"line_{ln:04d}.wav"
        if not take_wav.is_file() or take_wav.stat().st_size <= 1000:
            missing_takes.append(ln)
            continue
        tok, m = by_line[ln]
        audio, sr = sf.read(str(take_wav), dtype="float32", always_2d=True)
        mono = audio[:, 0] if audio.shape[1] == 1 else audio.mean(axis=1)
        raw_dur = len(mono) / float(sr)
        s0, s1 = _trim_mic_take_silence(mono, sr, pad_s=0.18)
        trimmed = mono[s0:s1]
        trim_dur = len(trimmed) / float(sr)

        fname = f"line_{ln:04d}.wav"
        sf.write(str(project.segments_dir / fname), trimmed, sr, subtype="PCM_16")
        sf.write(str(pass2_dir / fname), trimmed, sr, subtype="PCM_16")
        (pass2_dir / f"line_{ln:04d}.lab").write_text(f"{tok}\n", encoding="utf-8")
        rows_by_line[ln] = {
            "line": ln,
            "token": tok,
            "kind": m["kind"],
            "text": m["text"],
            "source_wav": f"takes/{fname}",
            "source_start_s": f"{s0 / float(sr):.6f}",
            "source_end_s": f"{s1 / float(sr):.6f}",
            "duration_s": f"{trim_dur:.6f}",
            "pass1_raw_dur_s": f"{raw_dur:.4f}",
            "trimmed_dur_s": f"{trim_dur:.4f}",
            "segment_wav": fname,
        }
        prepared_count += 1

    if prepared_count == 0:
        raise FileNotFoundError(
            f"No recorded mic takes found to align (missing {len(missing_takes)} line(s): {missing_takes[:15]})."
        )

    if progress:
        miss_note = f" ({len(missing_takes)} skipped/unrecorded)" if missing_takes else ""
        progress(f"Preparing {prepared_count} mic take(s){miss_note} for Pass-2 MFA...", 0.14)
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
        num_jobs=min(num_jobs, max(1, prepared_count)),
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

    report = audit_aligned_clips(project, full_meta, warnings=[])
    report.phone_map_approximate = pm.approximate
    project.set_stage("align", "done" if not report.flags else "flagged")
    if progress:
        progress(f"Alignment finished: {report.aligned_lines}/{report.total_lines} lines aligned.", 1.0)
    return report


def _trim_outer_in_window(
    frame_db: np.ndarray,
    t0: float,
    t1: float,
    speech_thresh: float,
) -> Tuple[float, float]:
    """Trim outer silence inside `[t0, t1]` using sustained speech (>= 3 active 10 ms frames)."""
    f0 = max(0, int(t0 / 0.010))
    f1 = min(len(frame_db), int(math.ceil(t1 / 0.010)))
    if f1 <= f0:
        return t0, t1
    mask = (frame_db[f0:f1] > speech_thresh).astype(np.int32)
    if len(mask) >= 4:
        runs = np.convolve(mask, np.ones(4, dtype=np.int32), mode="same")
        sustained = np.flatnonzero((mask > 0) & (runs >= 3))
    else:
        sustained = np.flatnonzero(mask > 0)
    act = sustained if len(sustained) > 0 else np.flatnonzero(mask > 0)
    if len(act) == 0:
        return t0, t1
    a0 = (f0 + int(act[0])) * 0.010
    a1 = (f0 + int(act[-1])) * 0.010 + 0.020
    return max(t0, a0 - 0.040), min(t1, a1 + 0.040)


def _check_pass1_drift(
    wav_name: str,
    mapped_words: Sequence[Tuple[str, float, float]],
    strict_utts: Sequence[Tuple[float, float, float]],
    mono: np.ndarray,
    sr: int,
    noise_p10: float,
    token_meta: Dict[str, Dict[str, object]],
) -> List[str]:
    """Post-Pass-1 sanity checks catching beam collapse, abandoned audio tails, or swallowed words."""
    warnings: List[str] = []
    if not mapped_words:
        warnings.append(f"{wav_name}: Pass-1 produced 0 aligned words.")
        return warnings

    # 1. Check for abandoned trailing spoken audio (> max(5%, 10 s) of utterances left unaligned)
    if strict_utts:
        last_speech_end = strict_utts[-1][1]
        first_speech_start = strict_utts[0][0]
        speech_span = max(1.0, last_speech_end - first_speech_start)
        last_aligned_end = max(re_ for _, _, re_ in mapped_words)
        unaligned_tail = last_speech_end - last_aligned_end
        if unaligned_tail > max(10.0, 0.05 * speech_span):
            warnings.append(
                f"{wav_name}: Pass-1 alignment stopped at {last_aligned_end:.1f} s, leaving "
                f"{unaligned_tail:.1f} s of spoken audio unaligned (last speech at {last_speech_end:.1f} s). "
                "Check for skipped words or range mismatch."
            )

    # 2. Check for words swallowing multiple utterances (> 2.8 s and overlapping >= 2 strict utterances)
    swallowed: List[Tuple[int, float, float]] = []
    for tok, rs, re_ in mapped_words:
        if (re_ - rs) > 2.8:
            ov = sum(1 for u0, u1, _ in strict_utts if min(re_, u1) - max(rs, u0) > 0.12)
            if ov >= 2:
                ln = int(token_meta[tok]["line"]) if tok in token_meta else -1
                swallowed.append((ln, rs, re_ - rs))
    if len(swallowed) >= max(3, int(0.03 * len(mapped_words))):
        ln0, t0, d0 = swallowed[0]
        warnings.append(
            f"{wav_name}: {len(swallowed)} aligned word(s) swallowed multiple spoken utterances "
            f"(first at line {ln0}, {t0:.1f} s, span {d0:.1f} s) — indicates alignment drift."
        )

    # 3. Check for many words landing on pure silence
    silent_lines: List[Tuple[int, float]] = []
    for tok, rs, re_ in mapped_words:
        s0 = max(0, int(rs * sr))
        s1 = min(len(mono), int(re_ * sr))
        if s1 <= s0:
            rms_db = -120.0
        else:
            rms_db = 20.0 * math.log10(max(float(np.sqrt(np.mean(mono[s0:s1].astype(np.float64) ** 2))), 1e-12))
        if rms_db < noise_p10 + 8.0:
            ln = int(token_meta[tok]["line"]) if tok in token_meta else -1
            silent_lines.append((ln, rs))
    if len(silent_lines) >= max(5, int(0.05 * len(mapped_words))):
        ln0, t0 = silent_lines[0]
        warnings.append(
            f"{wav_name}: {len(silent_lines)} word(s) aligned onto silence (first at line {ln0}, {t0:.1f} s)."
        )

    return warnings


def _slice_mapped_words_to_clips(
    mono: np.ndarray,
    sr: int,
    rel_source_wav: str,
    mapped_words: Sequence[Tuple[str, float, float]],
    token_meta: Dict[str, Dict[str, object]],
    segments_dir: Path,
    pass2_dir: Path,
    *,
    pad_s: float = 0.14,
    noise_p10: Optional[float] = None,
    speech_ref: Optional[float] = None,
) -> Dict[int, Dict[str, object]]:
    """Trim outer silence using speech-anchored threshold, slice exact clips from `mono`, write Pass-2 corpus."""
    total_dur = len(mono) / float(sr)
    frame_db, calc_p10 = compute_frame_db(mono, sr)
    np10 = calc_p10 if noise_p10 is None else noise_p10
    if speech_ref is None:
        above = frame_db[frame_db > np10 + 12.0]
        sref = float(np.percentile(above, 85)) if len(above) >= 10 else float(np.percentile(frame_db, 95))
    else:
        sref = speech_ref
    # Anchor to speech level (avoids -52 dB breath leaks on quiet external recorders)
    speech_thresh = max(np10 + 10.0, sref - 24.0)

    trimmed: List[Tuple[str, float, float, float, float]] = []
    for tok, rs, re_ in mapped_words:
        if tok not in token_meta:
            continue
        ts, te = _trim_outer_in_window(frame_db, rs, re_, speech_thresh)
        trimmed.append((tok, rs, re_, ts, te))

    rows: Dict[int, Dict[str, object]] = {}
    for i, (tok, rs, re_, ts, te) in enumerate(trimmed):
        m = token_meta[tok]
        ln = int(m["line"])
        prev_end = trimmed[i - 1][4] if i > 0 else 0.0
        next_start = trimmed[i + 1][3] if i + 1 < len(trimmed) else total_dur
        clip_s = max(0.0, ts - pad_s, 0.5 * (prev_end + ts) if i > 0 else 0.0)
        clip_e = min(total_dur, te + pad_s, 0.5 * (te + next_start) if i + 1 < len(trimmed) else total_dur)
        if clip_e <= clip_s:
            clip_s = max(0.0, min(total_dur, rs))
            clip_e = max(clip_s + 0.02, min(total_dur, re_))

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


def _slice_from_vad_utterances(
    mono: np.ndarray,
    sr: int,
    rel_source_wav: str,
    utterances: Sequence[Tuple[float, float, float]],
    ordered_lines: Sequence[int],
    by_line: Dict[int, Tuple[str, Dict[str, object]]],
    segments_dir: Path,
    pass2_dir: Path,
    *,
    pad_s: float = 0.14,
) -> Dict[int, Dict[str, object]]:
    """Slice isolated 1-to-1 spoken utterances directly from `mono` into Pass-2 clips (zero Pass-1 drift)."""
    total_dur = len(mono) / float(sr)
    rows: Dict[int, Dict[str, object]] = {}
    for i, ln in enumerate(ordered_lines):
        tok, m = by_line[ln]
        ts, te, _ = utterances[i]
        prev_end = utterances[i - 1][1] if i > 0 else 0.0
        next_start = utterances[i + 1][0] if i + 1 < len(utterances) else total_dur
        clip_s = max(0.0, ts - pad_s, 0.5 * (prev_end + ts) if i > 0 else 0.0)
        clip_e = min(total_dur, te + pad_s, 0.5 * (te + next_start) if i + 1 < len(utterances) else total_dur)

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
            "pass1_raw_dur_s": f"{te - ts:.4f}",
            "trimmed_dur_s": f"{te - ts:.4f}",
            "segment_wav": fname,
        }
    return rows


def _plan_vad_pass1_chunks(
    strict_utts: Sequence[Tuple[float, float, float]],
    loose_utts: Sequence[Tuple[float, float, float]],
    ordered_lines: Sequence[int],
    by_line: Dict[int, Tuple[str, Dict[str, object]]],
    *,
    target_chunk_lines: int = 50,
) -> List[Tuple[int, int, int, int]]:
    """Split `(ordered_lines, loose_utts)` into ~50-word chunks only at high-confidence shift-verified boundaries.

    Returns list of `(line_start, line_end, loose_start, loose_end)` half-open index tuples.
    """
    n_lines = len(ordered_lines)
    n_loose = len(loose_utts)
    if n_lines <= 65 or len(strict_utts) < 25:
        return [(0, n_lines, 0, n_loose)]

    # Map each strict utterance to its matching loose utterance index
    strict_to_loose: List[int] = []
    l_ptr = 0
    for s0, s1, _ in strict_utts:
        while l_ptr + 1 < n_loose and loose_utts[l_ptr][1] < s0 - 0.02:
            l_ptr += 1
        best_l = l_ptr
        best_ov = -1.0
        for cand in range(max(0, l_ptr - 1), min(n_loose, l_ptr + 3)):
            ov = min(s1, loose_utts[cand][1]) - max(s0, loose_utts[cand][0])
            if ov > best_ov:
                best_ov = ov
                best_l = cand
        strict_to_loose.append(best_l)

    durs = np.array([e - s for s, e, _ in strict_utts], dtype=np.float64)
    nph = np.array([max(1, len(by_line[ln][1]["phones"])) for ln in ordered_lines], dtype=np.float64)
    m_strict = len(strict_utts)
    max_sh = max(2, min(12, abs(m_strict - n_lines) + 3))

    def _window_shift(j_center: int, half_w: int = 16) -> Optional[int]:
        """Return confident shift `sh = line_idx - strict_idx` centered around line `j_center`, or None."""
        scores: List[Tuple[float, int]] = []
        for sh in range(-max_sh, max_sh + 1):
            s_c = j_center - sh
            s0 = max(0, s_c - half_w)
            s1 = min(m_strict, s_c + half_w)
            j0 = s0 + sh
            j1 = s1 + sh
            if j0 < 0:
                s0 -= j0
                j0 = 0
            if j1 > n_lines:
                s1 -= (j1 - n_lines)
                j1 = n_lines
            if (s1 - s0) < 14:
                continue
            r = _safe_corr(durs[s0:s1], nph[j0:j1])
            scores.append((r, sh))
        if not scores:
            return None
        scores.sort(reverse=True)
        best_r, best_sh = scores[0]
        second_r = scores[1][0] if len(scores) > 1 else -1.0
        if best_r >= 0.35 and (best_r - second_r) >= 0.14:
            return best_sh
        return None

    cuts: List[Tuple[int, int]] = [(0, 0)]
    j_next = target_chunk_lines
    while j_next <= n_lines - 25:
        placed = False
        for delta_j in (0, -6, 6, -12, 12):
            jc = j_next + delta_j
            if jc <= cuts[-1][0] + 20 or jc >= n_lines - 20:
                continue
            sh_l = _window_shift(jc - 10, half_w=14)
            sh_c = _window_shift(jc, half_w=14)
            sh_r = _window_shift(jc + 10, half_w=14)
            if sh_c is not None and sh_l == sh_c and sh_r == sh_c:
                s_idx = jc - sh_c
                if 0 < s_idx < m_strict:
                    l_idx = strict_to_loose[s_idx]
                    if l_idx > cuts[-1][1] and l_idx < n_loose:
                        cuts.append((jc, l_idx))
                        j_next = jc + target_chunk_lines
                        placed = True
                        break
        if not placed:
            j_next += target_chunk_lines

    cuts.append((n_lines, n_loose))
    return [
        (cuts[k][0], cuts[k + 1][0], cuts[k][1], cuts[k + 1][1])
        for k in range(len(cuts) - 1)
    ]


def _align_wav_pass1(
    mfa_env: Path,
    model_name: str,
    dict_path: Path,
    wav_path: Path,
    rel_source_wav: str,
    ordered_lines: Sequence[int],
    by_line: Dict[int, Tuple[str, Dict[str, object]]],
    full_meta: Dict[str, Dict[str, object]],
    inspection: ExternalWavInspection,
    work_dir: Path,
    segments_dir: Path,
    pass2_dir: Path,
    *,
    tag: str,
    pad_s: float = 0.14,
    num_jobs: int = 4,
    progress: Optional[ProgressFn] = None,
    cancel: Optional[CancelFn] = None,
    progress_start: float = 0.14,
    progress_end: float = 0.54,
) -> Tuple[Dict[int, Dict[str, object]], List[str]]:
    """Align one external/pickup WAV into per-line clips via Direct-VAD (exact) or Condensed Chunked Pass-1."""
    audio, sr = sf.read(str(wav_path), dtype="float32", always_2d=True)
    mono = audio[:, 0] if audio.shape[1] == 1 else audio.mean(axis=1)
    total_dur = len(mono) / float(sr) if sr > 0 else 0.0
    warnings: List[str] = []

    # --- Tier 1: Exact isolated-word match -> Direct VAD Slicing (skip Pass-1 completely!) ---
    if inspection.direct_vad_ok:
        if progress:
            progress(
                f"{wav_path.name}: {inspection.detected_count}/{inspection.expected_count} spoken words "
                f"matched 1-to-1 — slicing directly (drift-free)...",
                progress_end,
            )
        rows = _slice_from_vad_utterances(
            mono,
            sr,
            rel_source_wav,
            inspection.utterances,
            ordered_lines,
            by_line,
            segments_dir,
            pass2_dir,
            pad_s=pad_s,
        )
        return rows, warnings

    # Record pre-flight mismatch/shift info as a helpful warning
    if inspection.delta != 0 or inspection.shift_warning:
        detail = f" {inspection.shift_warning}" if inspection.shift_warning else ""
        warnings.append(
            f"{wav_path.name}: detected {inspection.detected_count} spoken utterance(s) for "
            f"{inspection.expected_count} expected line(s) ({inspection.delta:+d}).{detail}"
        )

    whole_dir = work_dir / f"{tag}_whole"
    aligned_whole = work_dir / f"{tag}_aligned"
    for d in (whole_dir, aligned_whole):
        if d.exists():
            shutil.rmtree(d)
        d.mkdir(parents=True, exist_ok=True)

    n_lines = len(ordered_lines)
    loose = inspection.loose_utterances
    use_condensed = (
        len(loose) >= max(1, int(0.50 * n_lines))
        and abs(len(loose) - n_lines) <= max(15, int(0.25 * n_lines))
    )

    g = math.gcd(16000, sr)
    chunk_seg_maps: Dict[str, List[Tuple[float, float, float, float]]] = {}

    if use_condensed:
        chunks = _plan_vad_pass1_chunks(inspection.utterances, loose, ordered_lines, by_line)
        if progress:
            progress(
                f"{wav_path.name}: mismatch detected ({inspection.detected_count}/{inspection.expected_count}) — "
                f"preparing {len(chunks)} pause-condensed Pass-1 chunk(s)...",
                progress_start,
            )
        for ci, (j0, j1, l0, l1) in enumerate(chunks):
            cname = f"{tag}_c{ci:02d}"
            pieces: List[np.ndarray] = []
            seg_map: List[Tuple[float, float, float, float]] = []
            cond_cursor = 0.0
            for k in range(l0, l1):
                u0, u1, _ = loose[k]
                prev_u1 = loose[k - 1][1] if k > 0 else 0.0
                next_u0 = loose[k + 1][0] if k + 1 < len(loose) else total_dur
                s_t = max(0.0, u0 - 0.11, 0.5 * (prev_u1 + u0) if k > 0 else 0.0)
                e_t = min(total_dur, u1 + 0.11, 0.5 * (u1 + next_u0) if k + 1 < len(loose) else total_dur)
                s_i = int(round(s_t * sr))
                e_i = int(round(e_t * sr))
                seg = mono[s_i:e_i]
                seg16 = resample_poly(seg, 16000 // g, sr // g).astype(np.float32) if sr != 16000 else seg
                seg_dur = len(seg16) / 16000.0
                pieces.append(seg16)
                seg_map.append((cond_cursor, cond_cursor + seg_dur, s_i / float(sr), e_i / float(sr)))
                cond_cursor += seg_dur
            chunk_audio = np.concatenate(pieces) if pieces else np.zeros(1600, dtype=np.float32)
            sf.write(str(whole_dir / f"{cname}.wav"), chunk_audio, 16000, subtype="PCM_16")
            toks = [by_line[ln][0] for ln in ordered_lines[j0:j1]]
            (whole_dir / f"{cname}.lab").write_text(" ".join(toks) + "\n", encoding="utf-8")
            chunk_seg_maps[cname] = seg_map
        p1_jobs = min(max(1, num_jobs), max(1, len(chunks)))
    else:
        # Tier 3: Continuous speech or severe mismatch -> whole-file Pass-1 with fixed trimmer & drift check
        if progress:
            progress(f"Resampling {wav_path.name} to 16 kHz mono for Pass-1 MFA...", progress_start)
        mono16 = resample_poly(mono, 16000 // g, sr // g).astype(np.float32) if sr != 16000 else mono
        sf.write(str(whole_dir / f"{tag}.wav"), mono16, 16000, subtype="PCM_16")
        toks = [by_line[ln][0] for ln in ordered_lines]
        (whole_dir / f"{tag}.lab").write_text(" ".join(toks) + "\n", encoding="utf-8")
        p1_jobs = 1

    _run_mfa_align(
        mfa_env,
        whole_dir,
        dict_path,
        model_name,
        aligned_whole,
        num_jobs=p1_jobs,
        beam=100,
        retry_beam=400,
        progress=progress,
        cancel=cancel,
        label=f"Pass-1 MFA ({wav_path.name})",
        progress_start=progress_start + 0.02,
        progress_end=progress_end - 0.02,
    )

    mapped_words: List[Tuple[str, float, float]] = []
    if use_condensed:
        def _map_condensed_span(c0: float, c1: float, smap: Sequence[Tuple[float, float, float, float]]) -> Tuple[float, float]:
            if not smap:
                return c0, c1
            overlaps: List[Tuple[float, int]] = []
            for idx_s, (cs0, cs1, _os0, _os1) in enumerate(smap):
                ov = min(c1, cs1) - max(c0, cs0)
                if ov > 0.0:
                    overlaps.append((ov, idx_s))
            if not overlaps:
                if c1 <= smap[0][0]:
                    return smap[0][2], smap[0][2] + 0.02
                return smap[-1][3] - 0.02, smap[-1][3]
            substantial = [idx_s for ov, idx_s in overlaps if ov >= 0.08]
            if not substantial:
                best_idx = max(overlaps, key=lambda x: x[0])[1]
                substantial = [best_idx]
            first_k = substantial[0]
            last_k = substantial[-1]
            cs0_f, cs1_f, os0_f, os1_f = smap[first_k]
            cs0_l, cs1_l, os0_l, os1_l = smap[last_k]
            frac0 = max(0.0, min(1.0, (c0 - cs0_f) / max(1e-6, cs1_f - cs0_f)))
            frac1 = max(0.0, min(1.0, (c1 - cs0_l) / max(1e-6, cs1_l - cs0_l)))
            return os0_f + frac0 * (os1_f - os0_f), os0_l + frac1 * (os1_l - os0_l)

        for cname in sorted(chunk_seg_maps):
            tg_p = aligned_whole / f"{cname}.TextGrid"
            if not tg_p.is_file():
                warnings.append(f"{wav_path.name}: Pass-1 chunk {cname} did not produce a TextGrid.")
                continue
            tiers = parse_textgrid(tg_p)
            smap = chunk_seg_maps[cname]
            for wiv in tiers.get("words", []):
                tok = str(wiv["text"])
                if not tok or tok not in full_meta:
                    continue
                os0, os1 = _map_condensed_span(float(wiv["xmin"]), float(wiv["xmax"]), smap)
                mapped_words.append((tok, os0, os1))
    else:
        tg_p = aligned_whole / f"{tag}.TextGrid"
        if not tg_p.is_file():
            raise RuntimeError(f"Pass-1 MFA did not produce TextGrid for {wav_path.name}")
        tiers = parse_textgrid(tg_p)
        for wiv in tiers.get("words", []):
            tok = str(wiv["text"])
            if tok and tok in full_meta:
                mapped_words.append((tok, float(wiv["xmin"]), float(wiv["xmax"])))

    drift_warnings = _check_pass1_drift(
        wav_path.name,
        mapped_words,
        inspection.utterances,
        mono,
        sr,
        inspection.noise_p10_db,
        full_meta,
    )
    warnings.extend(drift_warnings)

    rows = _slice_mapped_words_to_clips(
        mono,
        sr,
        rel_source_wav,
        mapped_words,
        full_meta,
        segments_dir,
        pass2_dir,
        pad_s=pad_s,
        noise_p10=inspection.noise_p10_db,
        speech_ref=inspection.speech_ref_db,
    )
    return rows, warnings


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
    mapped = [
        (str(iv["text"]), float(iv["xmin"]), float(iv["xmax"]))
        for iv in tiers.get("words", [])
        if iv["text"] and str(iv["text"]) in token_meta
    ]
    audio, sr = sf.read(str(src_wav_path), dtype="float32", always_2d=True)
    mono = audio[:, 0] if audio.shape[1] == 1 else audio.mean(axis=1)
    return _slice_mapped_words_to_clips(
        mono,
        sr,
        rel_source_wav,
        mapped,
        token_meta,
        segments_dir,
        pass2_dir,
        pad_s=pad_s,
    )


def align_external_recordings(
    project: VoiceProject,
    *,
    num_jobs_pass2: int = 4,
    pad_s: float = 0.14,
    progress: Optional[ProgressFn] = None,
    cancel: Optional[CancelFn] = None,
) -> AlignmentReport:
    """Drift-proof alignment for external recorder WAV(s): Direct-VAD (exact) or Chunked Pass-1 -> Pass-2 MFA."""
    if not project.ranges_file.is_file():
        raise FileNotFoundError(f"Missing external range map: {project.ranges_file}")

    mfa_env, pm = _prepare_mfa_and_map(project, progress=progress, cancel=cancel)
    dict_lines, full_meta = _build_mfa_dict_and_meta(project, pm)
    by_line = {int(v["line"]): (tok, v) for tok, v in full_meta.items()}

    raw_ranges = read_ranges_file(project.ranges_file)
    ranges = validate_external_ranges(raw_ranges, project.external_dir, sorted(by_line))

    dict_path = project.align_dir / "mfa_dict.txt"
    dict_path.write_text("\n".join(dict_lines) + "\n", encoding="utf-8")
    project.token_meta_file.write_text(json.dumps(full_meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    pass1_work = project.align_dir / "pass1_work"
    if pass1_work.exists():
        shutil.rmtree(pass1_work)
    pass1_work.mkdir(parents=True, exist_ok=True)

    pass2_dir = project.align_dir / "mfa_pass2_corpus"
    if pass2_dir.exists():
        shutil.rmtree(pass2_dir)
    pass2_dir.mkdir(parents=True, exist_ok=True)
    project.segments_dir.mkdir(parents=True, exist_ok=True)
    # Clear stale per-line TextGrids before a full external alignment
    if project.mfa_aligned_dir.exists():
        shutil.rmtree(project.mfa_aligned_dir)
    project.mfa_aligned_dir.mkdir(parents=True, exist_ok=True)

    phone_counts = {ln: len(v["phones"]) for ln, (_tok, v) in by_line.items()}
    line_texts = {ln: str(v["text"]) for ln, (_tok, v) in by_line.items()}

    rows_by_line: Dict[int, Dict[str, object]] = {}
    all_warnings: List[str] = []
    n_ranges = max(1, len(ranges))
    for r_i, (wav_name, lo, hi) in enumerate(ranges):
        if cancel and cancel():
            raise CancelledError("Alignment cancelled by user.")
        src_path = project.external_dir / wav_name
        ordered_lines = list(range(lo, hi + 1))
        p_start = 0.08 + 0.48 * (r_i / float(n_ranges))
        p_end = 0.08 + 0.48 * ((r_i + 1) / float(n_ranges))
        if progress:
            progress(f"Inspecting spoken utterances in {wav_name} (lines {lo}-{hi})...", p_start)
        insp = inspect_external_wav(
            src_path,
            ordered_lines,
            line_phone_counts=phone_counts,
            line_texts=line_texts,
        )
        sliced, wav_warnings = _align_wav_pass1(
            mfa_env,
            project.config.mfa_model,
            dict_path,
            src_path,
            f"external/{wav_name}",
            ordered_lines,
            by_line,
            full_meta,
            insp,
            pass1_work,
            project.segments_dir,
            pass2_dir,
            tag=f"r{r_i:02d}_{Path(wav_name).stem}",
            pad_s=pad_s,
            num_jobs=num_jobs_pass2,
            progress=progress,
            cancel=cancel,
            progress_start=p_start + 0.01,
            progress_end=p_end,
        )
        rows_by_line.update(sliced)
        all_warnings.extend(wav_warnings)

    _save_segments_csv(project.segments_csv, rows_by_line)

    _run_mfa_align(
        mfa_env,
        pass2_dir,
        dict_path,
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
    report = audit_aligned_clips(project, full_meta, warnings=all_warnings)
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
    pass2_dir = work / "mfa_pass2_corpus"
    aligned_p2 = work / "mfa_aligned"
    for d in (pass2_dir, aligned_p2):
        d.mkdir(parents=True, exist_ok=True)
    project.segments_dir.mkdir(parents=True, exist_ok=True)

    dict_path = work / "mfa_dict.txt"
    dict_path.write_text("\n".join(dict_lines) + "\n", encoding="utf-8")

    if progress:
        progress(f"Inspecting pickup/extension recording ({len(ordered)} line(s))...", 0.10)
    phone_counts = {ln: len(v["phones"]) for ln, (_tok, v) in by_line.items()}
    line_texts = {ln: str(v["text"]) for ln, (_tok, v) in by_line.items()}
    insp = inspect_external_wav(
        dest_wav,
        ordered,
        line_phone_counts=phone_counts,
        line_texts=line_texts,
    )

    patched_rows, pickup_warnings = _align_wav_pass1(
        mfa_env,
        project.config.mfa_model,
        dict_path,
        dest_wav,
        f"pickups/{dest_wav.name}",
        ordered,
        by_line,
        full_meta,
        insp,
        work,
        project.segments_dir,
        pass2_dir,
        tag=tag,
        pad_s=pad_s,
        num_jobs=1,
        progress=progress,
        cancel=cancel,
        progress_start=0.14,
        progress_end=0.54,
    )
    if not patched_rows:
        raise RuntimeError(f"Pickup alignment yielded 0 aligned words from {dest_wav.name}")

    _run_mfa_align(
        mfa_env,
        pass2_dir,
        dict_path,
        project.config.mfa_model,
        aligned_p2,
        num_jobs=min(4, max(1, len(patched_rows))),
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

    report = audit_aligned_clips(project, merged_meta, warnings=pickup_warnings)
    report.phone_map_approximate = pm.approximate
    project.set_stage("align", "done" if not report.flags else "flagged")
    if progress:
        progress(f"Patched {len(patched_rows)} line(s). Total aligned: {report.aligned_lines}/{report.total_lines}.", 1.0)
    return report
