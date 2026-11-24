#!/usr/bin/env python3
"""Language- and speaker-neutral MBROLA database & eSpeak voice builder.

Consumes the output of the hybrid word-corpus pipeline:
  - plan dir:     inventory.json + diphone_map.json (from plan_word_corpus.py)
  - segments CSV: line -> source_wav, source_start_s, source_end_s
  - aligned dir:  Pass-2 per-line MFA TextGrids + token_meta.json
  - audio dir:    read-only original WAV recordings (e.g. Voice_audio/)

Automatically:
  1. Extracts all diphone candidates from Pass-2 TextGrids with per-candidate
     SNR, RMS, peak, F0 stability, and medial/stressed positional scoring.
  2. Estimates speaker median F0 across voiced vowels and derives optimal
     MBROLATOR period, FrameShift, FrameLength, and eSpeak pitch range.
  3. Selects the best take per recorded diphone, derives word-edge (_X / X_)
     units from real room tone, and fills unrecorded matrix cells via
     IPA-driven articulatory nearest-donor cross-splicing.
  4. Runs anaf0 -> anambe -> resynth -> database_build -> full mbrola validation.
  5. Writes faithful eSpeak NG phtrans and mb-<name> voice descriptor.
"""

from __future__ import annotations

import argparse
import array
import collections
import csv
import json
import math
import os
import shutil
import statistics
import struct
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else SCRIPT_DIR.parent

if not getattr(sys, "frozen", False) and str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))
from resegment_from_whole_alignment import parse_textgrid  # noqa: E402


def _resolve_tool(flat_name: str, repo_rel: str) -> Path:
    flat = PROJECT_ROOT / "tools" / "mbrolator" / flat_name
    if flat.is_file():
        return flat
    return PROJECT_ROOT / repo_rel


ANAF0 = _resolve_tool("anaf0.exe", "tools/mbrolator-src/AnaMBE/Obj/anaf0.exe")
ANAMBE = _resolve_tool("anambe.exe", "tools/mbrolator-src/AnaMBE/Obj/anambe.exe")
RESYNTH = _resolve_tool("resynth.exe", "tools/mbrolator-src/Resynthesis/resynth.exe")
DATABASE_BUILD = _resolve_tool("database_build.exe", "tools/mbrolator-src/Resynthesis/database_build.exe")
MBROLA = _resolve_tool("mbrola.exe", "tools/mbrola-src/Bin/mbrola.exe")

# MBROLATOR analysis always keeps Freq=16000 even for 22.05 kHz databases;
# FrameShift/FrameLength/period scale to the speaker's F0 at the target rate,
# and only database_build receives the true sample rate.
F0_PARAMS = """Freq= 16000
FrameLength= 512
FrameShift= 80
"""
SYN_PARAMS = """Freq=16000
LastPhaseReset=10
VUVLimTra=0.55
VUVLimVoiced=1.2
ElimTrans=5e8
ElimVoiced=5e8
"""
DATABASE_VERSION = "2.069"
DATABASE_CODING = 1
DATABASE_VOLUME = 0.8
RESYNTH_GAIN = 3.5
MAX_PEAK_FRACTION = 0.92

PAD = 0.12
SILENCE_HALF = 0.10
TONE_LENGTH = 0.24
XFADE = 0.006

# Universal IPA articulatory features for language-neutral donor distance
# Vowels: (height 1=high..4=low, backness 1=front..3=back, rounded 0/1, long 0/1)
IPA_VOWEL_BASE = {
    "i": (1, 1, 0), "y": (1, 1, 1), "ɪ": (1.4, 1.2, 0), "ʏ": (1.4, 1.2, 1),
    "e": (2, 1, 0), "ø": (2, 1, 1), "ɛ": (3, 1, 0), "œ": (3, 1, 1),
    "æ": (3.7, 1, 0), "a": (4, 1.3, 0), "ɶ": (4, 1, 1),
    "ɨ": (1, 2, 0), "ʉ": (1, 2, 1), "ə": (2.5, 2, 0), "ɜ": (3, 2, 0),
    "ɞ": (3, 2, 1), "ɐ": (3.5, 2, 0),
    "ɯ": (1, 3, 0), "u": (1, 3, 1), "ʊ": (1.5, 2.7, 1),
    "ɤ": (2, 3, 0), "o": (2, 3, 1), "ʌ": (3, 2.7, 0), "ɔ": (3, 3, 1),
    "ɑ": (4, 3, 0), "ɒ": (4, 3, 1),
}

# Consonants: (place 1=bilabial..8=glottal, manner 1=stop,2=affricate,3=fricative,4=nasal,5=liquid,6=glide, voiced 0/1)
IPA_CONS_BASE = {
    "p": (1, 1, 0), "b": (1, 1, 1), "m": (1, 4, 1), "β": (1, 3, 1), "ɸ": (1, 3, 0),
    "f": (2, 3, 0), "v": (2, 3, 1), "ʋ": (2, 6, 1),
    "θ": (3, 3, 0), "ð": (3, 3, 1),
    "t": (4, 1, 0), "d": (4, 1, 1), "s": (4, 3, 0), "z": (4, 3, 1),
    "n": (4, 4, 1), "l": (4, 5, 1), "ɫ": (4.4, 5, 1), "r": (4, 5, 1), "ɹ": (4, 5, 1), "ɾ": (4, 5, 1),
    "ʃ": (5, 3, 0), "ʒ": (5, 3, 1), "tʃ": (5, 2, 0), "dʒ": (5, 2, 1), "ts": (4, 2, 0), "dz": (4, 2, 1),
    "c": (6, 1, 0), "ɟ": (6, 1, 1), "ç": (6, 3, 0), "ɲ": (6, 4, 1), "ʎ": (6, 5, 1), "j": (6, 6, 1),
    "k": (7, 1, 0), "ɡ": (7, 1, 1), "g": (7, 1, 1), "x": (7, 3, 0), "ɣ": (7, 3, 1), "ŋ": (7, 4, 1),
    "q": (7.5, 1, 0), "ʁ": (7.5, 3, 1),
    "h": (8, 3, 0), "ɦ": (8, 3, 1), "ʔ": (8, 1, 0),
    "w": (1.5, 6, 1),
}


def run(cmd: Sequence[object], cwd: Optional[Path] = None) -> subprocess.CompletedProcess:
    hidden_kw: Dict[str, object] = {}
    if os.name == "nt":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = subprocess.SW_HIDE
        hidden_kw["startupinfo"] = si
        hidden_kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    res = subprocess.run(
        [str(c) for c in cmd],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        **hidden_kw,
    )
    if res.returncode != 0:
        raise RuntimeError(
            f"command failed ({res.returncode}): {' '.join(str(c) for c in cmd)}\n"
            f"stdout:\n{res.stdout}\nstderr:\n{res.stderr}"
        )
    return res


def ipa_features(ipa: str, is_vowel: bool) -> Tuple[float, ...]:
    ipa_clean = ipa.strip()
    if is_vowel:
        long_flag = 1.0 if ("ː" in ipa_clean or len(ipa_clean.replace("ː", "")) >= 2) else 0.0
        for ch in ipa_clean:
            if ch in IPA_VOWEL_BASE:
                h, b, r = IPA_VOWEL_BASE[ch]
                return (h, b, float(r), long_flag)
        return (2.5, 2.0, 0.0, long_flag)
    if ipa_clean in IPA_CONS_BASE:
        return tuple(float(v) for v in IPA_CONS_BASE[ipa_clean])
    for k in (ipa_clean[:2], ipa_clean[:1]):
        if k in IPA_CONS_BASE:
            return tuple(float(v) for v in IPA_CONS_BASE[k])
    return (4.0, 3.0, 0.0)


def make_distance_fn(vowel_set: set[str], ipa_map: Dict[str, str]):
    feat_cache = {ph: ipa_features(ipa_map.get(ph, ph), ph in vowel_set) for ph in ipa_map}

    def dist(a: str, b: str) -> float:
        if a == b:
            return 0.0
        va, vb = a in vowel_set, b in vowel_set
        if va != vb:
            return 20.0
        fa = feat_cache.get(a)
        fb = feat_cache.get(b)
        if not fa or not fb:
            return 10.0
        if va:
            return abs(fa[0] - fb[0]) * 1.5 + abs(fa[1] - fb[1]) * 1.2 + abs(fa[2] - fb[2]) * 1.0 + abs(fa[3] - fb[3]) * 0.4
        return abs(fa[0] - fb[0]) * 1.4 + abs(fa[1] - fb[1]) * 1.6 + abs(fa[2] - fb[2]) * 1.2

    return dist


def estimate_f0_frames(seg: np.ndarray, sr: int, fmin: float = 65.0, fmax: float = 320.0) -> List[float]:
    """Pure-NumPy normalized autocorrelation F0 estimator (zero librosa/numba overhead)."""
    win_n = int(sr * 0.040)
    hop_n = int(sr * 0.010)
    if len(seg) < win_n:
        return []
    lag_min = max(1, int(sr / fmax))
    lag_max = min(win_n - 2, int(sr / fmin))
    if lag_max <= lag_min:
        return []
    seg64 = seg.astype(np.float64)
    ref_rms = float(np.sqrt(np.mean(seg64 ** 2)))
    if ref_rms < 1e-5:
        return []
    out: List[float] = []
    nfft = 1 << (2 * win_n - 1).bit_length()
    for pos in range(0, len(seg64) - win_n + 1, hop_n):
        frame = seg64[pos:pos + win_n]
        frame = frame - np.mean(frame)
        if float(np.sqrt(np.mean(frame ** 2))) < ref_rms * 0.45:
            continue
        spec = np.fft.rfft(frame, n=nfft)
        ac = np.fft.irfft(spec * np.conj(spec), n=nfft)[:lag_max + 1]
        if ac[0] <= 1e-12:
            continue
        ac = ac / ac[0]
        sub = ac[lag_min:lag_max + 1]
        best = int(np.argmax(sub))
        if sub[best] > 0.55:
            out.append(sr / float(lag_min + best))
    return out


def db_level(arr: np.ndarray) -> float:
    if len(arr) == 0:
        return -120.0
    rms = float(np.sqrt(np.mean(arr.astype(np.float64) ** 2)))
    return 20.0 * math.log10(max(rms, 1e-12))


def xfade(left: np.ndarray, right: np.ndarray, n: int) -> np.ndarray:
    t = np.linspace(0.0, 1.0, n, endpoint=False, dtype=np.float32)
    return left * np.cos(t * (math.pi / 2.0)) + right * np.sin(t * (math.pi / 2.0))


_INFO_CACHE: Dict[str, sf.SoundFile] = {}


def read_abs(path: Path, rate: int, t0: float, t1: float) -> np.ndarray:
    key = str(path)
    info = _INFO_CACHE.get(key)
    if info is None:
        info = sf.info(key)
        _INFO_CACHE[key] = info
    total = info.frames / float(info.samplerate)
    t0 = max(0.0, min(t0, total))
    t1 = max(t0, min(t1, total))
    start = int(round(t0 * rate))
    frames = max(1, int(round((t1 - t0) * rate)))
    frames = min(frames, max(1, info.frames - start))
    data, _ = sf.read(key, start=start, frames=frames, dtype="float32", always_2d=False)
    return np.asarray(data, dtype=np.float32).reshape(-1)


def read_abs_padded(path: Path, rate: int, t0: float, t1: float) -> np.ndarray:
    """Read [t0, t1) seconds on the source timeline, zero-padding past [0, total] so indices stay exact."""
    key = str(path)
    info = _INFO_CACHE.get(key)
    if info is None:
        info = sf.info(key)
        _INFO_CACHE[key] = info
    s0 = int(round(t0 * rate))
    s1 = int(round(t1 * rate))
    if s1 <= s0:
        s1 = s0 + 1
    n_total = s1 - s0
    r0 = max(0, min(s0, info.frames))
    r1 = max(r0, min(s1, info.frames))
    pre_pad = max(0, r0 - s0)
    if r1 > r0:
        data, _ = sf.read(key, start=r0, frames=r1 - r0, dtype="float32", always_2d=False)
        core = np.asarray(data, dtype=np.float32).reshape(-1)
    else:
        core = np.zeros(0, dtype=np.float32)
    post_pad = max(0, n_total - pre_pad - len(core))
    if pre_pad == 0 and post_pad == 0 and len(core) == n_total:
        return core
    parts: List[np.ndarray] = []
    if pre_pad > 0:
        parts.append(np.zeros(pre_pad, dtype=np.float32))
    if len(core) > 0:
        parts.append(core[: max(0, n_total - pre_pad)])
    if post_pad > 0:
        parts.append(np.zeros(post_pad, dtype=np.float32))
    return np.concatenate(parts) if parts else np.zeros(n_total, dtype=np.float32)


def prepare_sources(rate: int, work_dir: Path, audio_dir: Path, segments: Dict[int, Dict[str, str]]) -> Dict[str, Path]:
    """Convert read-only source WAV(s) once to mono at target sample rate using polyphase FIR."""
    conv_dir = work_dir / "conv"
    conv_dir.mkdir(parents=True, exist_ok=True)
    mapping: Dict[str, Path] = {}
    for source in sorted({r["source_wav"] for r in segments.values()}):
        src_path = audio_dir / source
        out_path = conv_dir / f"{Path(source).stem}_{rate}.wav"
        mapping[source] = out_path
        if out_path.exists() and out_path.stat().st_mtime >= src_path.stat().st_mtime:
            continue
        print(f"  resampling {source} -> {out_path.name} ({rate} Hz mono)", flush=True)
        audio, sr = sf.read(str(src_path), dtype="float32", always_2d=True)
        mono = audio[:, 0] if audio.shape[1] == 1 else audio.mean(axis=1)
        if sr == rate:
            out = mono
        else:
            g = math.gcd(rate, sr)
            out = resample_poly(mono, rate // g, sr // g).astype(np.float32)
        sf.write(str(out_path), out, rate, subtype="PCM_16")
    return mapping


def build_unit_grid(
    segments: Dict[int, Dict[str, str]],
    token_meta: Dict[str, Dict[str, object]],
    diphone_map: Dict[int, Dict[str, object]],
    aligned_dir: Path,
    audio_dir: Path,
    vowels: set[str],
    out_csv: Path,
) -> Tuple[List[Dict[str, object]], Dict[str, float]]:
    """Extract all diphone candidates from Pass-2 TextGrids and estimate speaker F0."""
    # Preload source audio once (mono 44.1 kHz) for fast SNR/RMS/F0 diagnostics
    src_cache: Dict[str, Tuple[np.ndarray, int]] = {}
    for seg in segments.values():
        sname = seg["source_wav"]
        if sname not in src_cache:
            a, sr = sf.read(str(audio_dir / sname), dtype="float32", always_2d=True)
            src_cache[sname] = (a[:, 0] if a.shape[1] == 1 else a.mean(axis=1), sr)

    rows: List[Dict[str, object]] = []
    corpus_vowel_f0: List[float] = []
    skipped = 0

    for ln in sorted(segments):
        seg = segments[ln]
        tg_path = aligned_dir / f"line_{ln:04d}.TextGrid"
        if not tg_path.exists():
            skipped += 1
            continue
        tiers = parse_textgrid(tg_path)
        wivs = [w for w in tiers.get("words", []) if w["text"]]
        pivs = [p for p in tiers.get("phones", []) if p["text"] and p["text"] not in ("sil", "sp", "spn")]
        if not wivs:
            skipped += 1
            continue
        wiv = wivs[0]
        tok = str(wiv["text"])
        m = token_meta.get(tok)
        if not m:
            skipped += 1
            continue
        word_start = float(wiv["xmin"])
        word_end = float(wiv["xmax"])
        w_phones = [
            p for p in pivs
            if (float(p["xmin"]) + float(p["xmax"])) * 0.5 >= word_start - 1e-4
            and (float(p["xmin"]) + float(p["xmax"])) * 0.5 <= word_end + 1e-4
        ]
        spans = m["spans"]
        expected_n = sum(int(cnt) for _, cnt in spans)
        if len(w_phones) != expected_n:
            skipped += 1
            continue

        # Recombine MFA phones back into canonical eSpeak phones
        canon: List[Tuple[str, float, float]] = []
        cursor = 0
        for ph, cnt in spans:
            sub = w_phones[cursor:cursor + int(cnt)]
            cursor += int(cnt)
            canon.append((str(ph), float(sub[0]["xmin"]), float(sub[-1]["xmax"])))

        stresses = diphone_map.get(ln, {}).get("stresses", [0] * len(canon))
        src_mono, sr = src_cache[seg["source_wav"]]
        src_start = float(seg["source_start_s"])

        # Estimate local room noise from 80ms before/after word
        pre_s = max(0, int((src_start + word_start - 0.12) * sr))
        pre_e = max(pre_s + 1, int((src_start + word_start - 0.02) * sr))
        post_s = min(len(src_mono) - 1, int((src_start + word_end + 0.02) * sr))
        post_e = min(len(src_mono), int((src_start + word_end + 0.12) * sr))
        noise_db = min(db_level(src_mono[pre_s:pre_e]), db_level(src_mono[post_s:post_e]))

        # Collect voiced vowel F0 frames across the whole corpus for true natural
        # conversational median F0 (avoids skewing upward into citation-form emphasis)
        n_ph = len(canon)
        for ph, ps, pe in canon:
            if ph in vowels and (pe - ps) >= 0.055:
                i0 = max(0, int((src_start + ps) * sr))
                i1 = min(len(src_mono), int((src_start + pe) * sr))
                corpus_vowel_f0.extend(estimate_f0_frames(src_mono[i0:i1], sr))

        # Extract every consecutive diphone pair in this word/supplement
        for i in range(n_ph - 1):
            p1, p1_s, p1_e = canon[i]
            p2, p2_s, p2_e = canon[i + 1]
            d1 = p1_e - p1_s
            d2 = p2_e - p2_s
            if d1 < 0.010 or d2 < 0.010:
                continue
            mid1 = p1_s + d1 / 2.0
            mid2 = p2_s + d2 / 2.0
            i0 = max(0, int((src_start + mid1) * sr))
            i1 = min(len(src_mono), int((src_start + mid2) * sr))
            seg_audio = src_mono[i0:i1]
            rms_db = db_level(seg_audio)
            peak = float(np.max(np.abs(seg_audio))) if len(seg_audio) else 0.0
            snr_db = rms_db - noise_db
            f0s = estimate_f0_frames(seg_audio, sr)
            f0_med = float(np.median(f0s)) if f0s else 0.0
            f0_std = float(np.std(f0s)) if len(f0s) >= 2 else 0.0

            is_stressed = int((i < len(stresses) and stresses[i] == 1) or (i + 1 < len(stresses) and stresses[i + 1] == 1))
            is_medial = int((0 < i) and (i + 1 < n_ph - 1))
            is_final = int(i + 1 == n_ph - 1)

            vocab_rank = int(diphone_map.get(ln, {}).get("vocab_rank", 99999))
            rows.append({
                "line": ln,
                "kind": str(m["kind"]),
                "text": str(m["text"]),
                "vocab_rank": vocab_rank,
                "source_wav": seg["source_wav"],
                "diphone": f"{p1}_{p2}",
                "phone1": p1,
                "phone2": p2,
                "word_start_s": round(word_start, 6),
                "word_end_s": round(word_end, 6),
                "phone1_start_s": round(p1_s, 6),
                "phone1_end_s": round(p1_e, 6),
                "phone2_start_s": round(p2_s, 6),
                "phone2_end_s": round(p2_e, 6),
                "phone1_duration_s": round(d1, 6),
                "phone2_duration_s": round(d2, 6),
                "snr_db": round(snr_db, 2),
                "rms_db": round(rms_db, 2),
                "peak": round(peak, 5),
                "f0_median_hz": round(f0_med, 2),
                "f0_std_hz": round(f0_std, 2),
                "is_stressed": is_stressed,
                "is_medial": is_medial,
                "is_final": is_final,
            })

    if not corpus_vowel_f0:
        raise RuntimeError("Could not estimate speaker F0 from aligned vowels")
    f0_stats = {
        "median_hz": round(float(np.median(corpus_vowel_f0)), 2),
        "p10_hz": round(float(np.percentile(corpus_vowel_f0, 10)), 2),
        "p90_hz": round(float(np.percentile(corpus_vowel_f0, 90)), 2),
        "voiced_frames": len(corpus_vowel_f0),
    }

    # Score every candidate take using a balanced, neutral-voice acoustic formula:
    #   1. Clean SNR floor + clipping guard (saturates at 28 dB)
    #   2. Natural conversational loudness centered on corpus median RMS (penalizing
    #      BOTH whispered/fading tails AND shouted/emphasized spikes)
    #   3. Anti-reduction steady-state duration clarity (saturates cleanly at 120 ms)
    #   4. Natural unemphasized speaking pitch near speaker's neutral median F0
    #      (penalizes creaky drops AND strongly accented high-pitch syllables that
    #      cause vocoder artifacts when synthesized at neutral pitch)
    #   5. Gentle stress & word-familiarity tie-breakers
    med_rms = statistics.median(float(r["rms_db"]) for r in rows)
    spk_f0 = f0_stats["median_hz"]

    for r in rows:
        snr = float(r["snr_db"])
        d1 = float(r["phone1_duration_s"])
        d2 = float(r["phone2_duration_s"])
        min_d = min(d1, d2)
        max_d = max(d1, d2)
        peak = float(r["peak"])
        rms = float(r["rms_db"])
        f0_med = float(r["f0_median_hz"])
        f0_std = float(r["f0_std_hz"])

        score = 0.0

        # 1. Clean SNR floor + clipping guard
        if snr < 18.0:
            score -= 2.0 * (18.0 - snr) / 6.0
        else:
            score += 0.60 * min(1.0, (snr - 18.0) / 10.0)
        if peak > 0.92:
            score -= 3.0

        # 2. Natural conversational loudness (penalize whispered drops & shouted spikes)
        rms_diff = rms - med_rms
        if rms_diff < -3.5:
            score -= min(1.4, 0.20 * (-3.5 - rms_diff))
        elif rms_diff > 5.0:
            score -= min(0.9, 0.16 * (rms_diff - 5.0))
        else:
            score += 0.30

        # 3. Anti-reduction & steady-state duration clarity (saturates cleanly at 120 ms)
        if min_d < 0.030:
            score -= 2.0
        elif min_d < 0.048:
            score -= 0.9
        elif min_d < 0.060:
            score -= 0.3

        dur_r1 = min(1.0, max(0.0, (d1 - 0.050) / 0.070))
        dur_r2 = min(1.0, max(0.0, (d2 - 0.050) / 0.070))
        score += 0.50 * (dur_r1 + dur_r2)
        if max_d > 0.360:
            score -= 0.5

        # 4. Natural unemphasized speaking pitch & steadiness (normalized by speaker F0)
        if f0_med > 0:
            ratio = f0_med / spk_f0
            if ratio < 0.85:
                score -= 0.7  # creaky drop
            elif ratio > 1.32:
                # Emphasized/accented high pitch (sounds vocoded when pitched back down)
                score -= min(1.0, 0.65 * (ratio - 1.32) / 0.20 + 0.25)
            elif 0.92 <= ratio <= 1.22:
                score += 0.35  # relaxed conversational speaking pitch

            rel_std = f0_std / spk_f0
            if rel_std > 0.03:
                score -= min(0.8, 0.65 * (rel_std - 0.03) / 0.06)

        # 5. Gentle stress & familiarity tie-breakers
        if r["is_stressed"]:
            score += 0.12
        if r["kind"] == "word":
            rank = int(r.get("vocab_rank", 99999))
            if rank <= 5000:
                score += 0.15
            elif rank > 15000:
                score -= 0.20
        else:
            score -= 0.30

        r["score"] = round(score, 4)

    out_csv.parent.mkdir(parents=True, exist_ok=True)
    with out_csv.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    print(f"Unit grid: {len(rows)} diphone takes across {len(segments) - skipped}/{len(segments)} aligned lines (skipped={skipped})", flush=True)
    print(f"Speaker F0 (from {f0_stats['voiced_frames']} vowel frames): median={f0_stats['median_hz']} Hz (p10={f0_stats['p10_hz']}, p90={f0_stats['p90_hz']})", flush=True)
    return rows, f0_stats


def select_takes(
    rows: List[Dict[str, object]],
    overrides: Optional[Dict[str, int]] = None,
) -> Tuple[Dict[str, Dict[str, object]], List[Dict[str, object]]]:
    by_label: Dict[str, List[Dict[str, object]]] = collections.defaultdict(list)
    for r in rows:
        by_label[str(r["diphone"])].append(r)
    ov = overrides or {}
    chosen: Dict[str, Dict[str, object]] = {}
    report: List[Dict[str, object]] = []
    for label, takes in sorted(by_label.items()):
        pinned_line = ov.get(label)
        pinned = next((t for t in takes if int(t["line"]) == pinned_line), None) if pinned_line is not None else None
        winner = pinned if pinned is not None else max(takes, key=lambda r: (float(r["score"]), -int(r["line"])))
        chosen[label] = winner
        report.append({
            "diphone": label,
            "line": int(winner["line"]),
            "text": winner["text"],
            "takes": len(takes),
            "score": float(winner["score"]),
            "snr_db": float(winner["snr_db"]),
            "f0_hz": float(winner["f0_median_hz"]),
            "stressed": int(winner["is_stressed"]),
            "medial": int(winner["is_medial"]),
            "pinned": int(pinned is not None),
        })
    return chosen, report


def natural_spec(label: str, row: Dict[str, object], seg: Dict[str, str]) -> Dict[str, object]:
    p1_s, p1_e = float(row["phone1_start_s"]), float(row["phone1_end_s"])
    p2_s, p2_e = float(row["phone2_start_s"]), float(row["phone2_end_s"])
    middle = p1_e if abs(p1_e - p2_s) < 1e-4 else p2_s
    return {
        "label": label,
        "kind": "natural",
        "left": str(row["phone1"]),
        "right": str(row["phone2"]),
        "line": int(row["line"]),
        "source_file": seg["source_wav"],
        "source_start": float(seg["source_start_s"]),
        "begin": p1_s + (p1_e - p1_s) / 2.0,
        "middle": middle,
        "end": p2_s + (p2_e - p2_s) / 2.0,
        "note": str(row["text"]),
    }


def _source_total_s(path: Path) -> float:
    key = str(path)
    info = _INFO_CACHE.get(key)
    if info is None:
        info = sf.info(key)
        _INFO_CACHE[key] = info
    return info.frames / float(info.samplerate)


def build_corpus_room_tone(
    rows: List[Dict[str, object]],
    segments: Dict[int, Dict[str, str]],
    sources: Dict[str, Path],
    rate: int,
    build_dir: Path,
) -> Path:
    """Extract a guaranteed TONE_LENGTH quiet room-tone clip across the corpus (or -68 dBFS floor)."""
    target_n = int(round(TONE_LENGTH * rate))
    min_short_n = max(64, int(round(0.04 * rate)))
    line_bounds: Dict[int, Tuple[float, float]] = {}
    for r in rows:
        ln = int(r["line"])
        if ln not in line_bounds:
            line_bounds[ln] = (float(r["word_start_s"]), float(r["word_end_s"]))

    best_full: Optional[Tuple[float, np.ndarray]] = None
    best_short: Optional[Tuple[float, np.ndarray]] = None

    for ln, (ws, we) in sorted(line_bounds.items()):
        seg = segments[ln]
        path = sources[seg["source_wav"]]
        total = _source_total_s(path)
        start = float(seg["source_start_s"])
        windows: List[Tuple[float, float]] = []
        pre_avail = start + ws
        if pre_avail >= 0.04:
            dur = min(TONE_LENGTH, pre_avail)
            windows.append((pre_avail - dur, pre_avail))
        post_start = start + we
        post_avail = total - post_start
        if post_avail >= 0.04:
            dur = min(TONE_LENGTH, post_avail)
            windows.append((post_start, post_start + dur))

        for t0, t1 in windows:
            arr = read_abs(path, rate, t0, t1)
            if len(arr) < min_short_n:
                continue
            lvl = db_level(arr)
            # Prefer real quiet room tone (-75..-38 dB) over pure digital zero if available
            rank_score = lvl if lvl > -85.0 else -42.0
            if len(arr) >= target_n - 4 and lvl <= -38.0:
                if best_full is None or rank_score < best_full[0]:
                    best_full = (rank_score, arr)
            elif lvl <= -35.0:
                short_score = rank_score - min(10.0, (len(arr) / float(target_n)) * 10.0)
                if best_short is None or short_score < best_short[0]:
                    best_short = (short_score, arr)

    if best_full is not None:
        arr = best_full[1]
        if len(arr) >= target_n:
            tone = arr[:target_n].astype(np.float32)
        else:
            tone = np.pad(arr.astype(np.float32), (0, target_n - len(arr)))
    elif best_short is not None:
        piece = best_short[1].astype(np.float32)
        ov = min(int(0.010 * rate), max(1, len(piece) // 4))
        tone = piece.copy()
        while len(tone) < target_n:
            if ov >= 4 and len(tone) > ov and len(piece) > ov:
                tone = np.concatenate([tone[:-ov], xfade(tone[-ov:], piece[:ov], ov), piece[ov:]])
            else:
                tone = np.concatenate([tone, piece])
        tone = tone[:target_n]
    else:
        rng = np.random.default_rng(12345)
        tone = rng.normal(0.0, 4e-4, size=target_n).astype(np.float32)

    room_tone_path = build_dir / "room_tone.wav"
    sf.write(str(room_tone_path), tone, rate, subtype="PCM_16")
    _INFO_CACHE.pop(str(room_tone_path), None)
    return room_tone_path


def splice_edge(
    phone: str,
    side: str,
    rows: List[Dict[str, object]],
    segments: Dict[int, Dict[str, str]],
    sources: Dict[str, Path],
    rate: int,
) -> Dict[str, object]:
    cands = [r for r in rows if r["phone1"] == phone or r["phone2"] == phone]
    cands.sort(key=lambda r: (-float(r["score"]), int(r["line"])))
    if side == "initial":
        row = next((r for r in cands if r["phone1"] == phone), cands[0])
        seg = segments[int(row["line"])]
        start = float(seg["source_start_s"])
        if row["phone1"] == phone:
            ps, pe = float(row["phone1_start_s"]), float(row["phone1_end_s"])
        else:
            ps, pe = float(row["phone2_start_s"]), float(row["phone2_end_s"])
        mid = ps + (pe - ps) / 2.0
        tone_end = start + float(row["word_start_s"])
        tone_start = tone_end - TONE_LENGTH
        src_path = sources[seg["source_wav"]]
        if tone_start >= 0.0 and db_level(read_abs(src_path, rate, tone_start, tone_end)) <= -40.0:
            tone_source = seg["source_wav"]
        else:
            tone_source = "__room_tone__"
            tone_start, tone_end = 0.0, TONE_LENGTH
        return {
            "label": f"_{phone}", "kind": "initial", "left": "_", "right": phone,
            "line": int(row["line"]), "source_file": seg["source_wav"], "source_start": start,
            "note": "spliced room tone + first half",
            "splice": {
                "mode": "tone_then_half", "source": seg["source_wav"],
                "tone_source": tone_source,
                "half_start": start + ps, "half_end": start + mid,
                "tone_start": tone_start, "tone_end": tone_end,
            },
        }
    row = next((r for r in cands if r["phone2"] == phone), cands[0])
    seg = segments[int(row["line"])]
    start = float(seg["source_start_s"])
    if row["phone2"] == phone:
        ps, pe = float(row["phone2_start_s"]), float(row["phone2_end_s"])
    else:
        ps, pe = float(row["phone1_start_s"]), float(row["phone1_end_s"])
    mid = ps + (pe - ps) / 2.0
    src_path = sources[seg["source_wav"]]
    total = _source_total_s(src_path)
    tone_start = start + float(row["word_end_s"])
    tone_end = tone_start + TONE_LENGTH
    if tone_end <= total and db_level(read_abs(src_path, rate, tone_start, tone_end)) <= -40.0:
        tone_source = seg["source_wav"]
    else:
        tone_source = "__room_tone__"
        tone_start, tone_end = 0.0, TONE_LENGTH
    return {
        "label": f"{phone}_", "kind": "final", "left": phone, "right": "_",
        "line": int(row["line"]), "source_file": seg["source_wav"], "source_start": start,
        "note": "spliced second half + room tone",
        "splice": {
            "mode": "half_then_tone", "source": seg["source_wav"],
            "tone_source": tone_source,
            "half_start": start + mid, "half_end": start + pe,
            "tone_start": tone_start, "tone_end": tone_end,
        },
    }


def edge_specs(
    phones: List[str],
    rows: List[Dict[str, object]],
    segments: Dict[int, Dict[str, str]],
    sources: Dict[str, Path],
    rate: int,
    build_dir: Optional[Path] = None,
) -> List[Dict[str, object]]:
    tone_dir = build_dir if build_dir is not None else next(iter(sources.values())).parent
    sources["__room_tone__"] = build_corpus_room_tone(rows, segments, sources, rate, tone_dir)

    initial: Dict[str, List[Dict[str, object]]] = collections.defaultdict(list)
    final: Dict[str, List[Dict[str, object]]] = collections.defaultdict(list)
    for r in rows:
        if abs(float(r["phone1_start_s"]) - float(r["word_start_s"])) < 1e-4:
            initial[str(r["phone1"])].append(r)
        if abs(float(r["phone2_end_s"]) - float(r["word_end_s"])) < 1e-4:
            final[str(r["phone2"])].append(r)

    min_sil_samples = int(0.9 * SILENCE_HALF * rate)

    def tone_db(r: Dict[str, object], side: str) -> float:
        seg = segments[int(r["line"])]
        path = sources[seg["source_wav"]]
        total = _source_total_s(path)
        start = float(seg["source_start_s"])
        if side == "pre":
            t1 = start + float(r["word_start_s"])
            t0 = t1 - SILENCE_HALF
            if t0 < 0.0:
                return 0.0
        else:
            t0 = start + float(r["word_end_s"])
            t1 = t0 + SILENCE_HALF
            if t1 > total:
                return 0.0
        arr = read_abs(path, rate, t0, t1)
        if len(arr) < min_sil_samples:
            return 0.0
        return db_level(arr)

    specs: List[Dict[str, object]] = []
    for ph in phones:
        best = None
        for r in initial.get(ph, []):
            tone = tone_db(r, "pre")
            span = float(r["phone1_duration_s"]) / 2.0
            key = (tone <= -45.0 and span >= 0.015, tone <= -40.0 and span >= 0.015, float(r["score"]) + span * 10.0, -tone)
            if best is None or key > best[0]:
                best = (key, r)
        if best is not None and best[0][1]:
            r = best[1]
            seg = segments[int(r["line"])]
            we = float(r["word_start_s"])
            ps, pe = float(r["phone1_start_s"]), float(r["phone1_end_s"])
            specs.append({
                "label": f"_{ph}", "kind": "initial", "left": "_", "right": ph,
                "line": int(r["line"]), "source_file": seg["source_wav"],
                "source_start": float(seg["source_start_s"]),
                "begin": we - SILENCE_HALF, "middle": we, "end": ps + (pe - ps) / 2.0,
                "note": f"recorded room tone ({r['text']})",
            })
        else:
            specs.append(splice_edge(ph, "initial", rows, segments, sources, rate))

        best = None
        for r in final.get(ph, []):
            tone = tone_db(r, "post")
            span = float(r["phone2_duration_s"]) / 2.0
            key = (tone <= -45.0 and span >= 0.015, tone <= -40.0 and span >= 0.015, float(r["score"]) + span * 10.0, -tone)
            if best is None or key > best[0]:
                best = (key, r)
        if best is not None and best[0][1]:
            r = best[1]
            seg = segments[int(r["line"])]
            we = float(r["word_end_s"])
            ps, pe = float(r["phone2_start_s"]), float(r["phone2_end_s"])
            specs.append({
                "label": f"{ph}_", "kind": "final", "left": ph, "right": "_",
                "line": int(r["line"]), "source_file": seg["source_wav"],
                "source_start": float(seg["source_start_s"]),
                "begin": ps + (pe - ps) / 2.0, "middle": we, "end": we + SILENCE_HALF,
                "note": f"recorded room tone ({r['text']})",
            })
        else:
            specs.append(splice_edge(ph, "final", rows, segments, sources, rate))
    return specs


def fallback_specs(
    phones: List[str],
    rows: List[Dict[str, object]],
    segments: Dict[int, Dict[str, str]],
    chosen: Dict[str, Dict[str, object]],
    dist_fn,
) -> List[Dict[str, object]]:
    by_left: Dict[str, List[Dict[str, object]]] = collections.defaultdict(list)
    by_right: Dict[str, List[Dict[str, object]]] = collections.defaultdict(list)
    for label, r in chosen.items():
        seg = segments[int(r["line"])]
        start = float(seg["source_start_s"])
        p1_s, p1_e = float(r["phone1_start_s"]), float(r["phone1_end_s"])
        p2_s, p2_e = float(r["phone2_start_s"]), float(r["phone2_end_s"])
        by_left[str(r["phone1"])].append({
            "row": r, "source_file": seg["source_wav"], "score": float(r["score"]),
            "mid": start + p1_s + (p1_e - p1_s) / 2.0, "edge": start + p1_e, "other": str(r["phone2"]),
        })
        by_right[str(r["phone2"])].append({
            "row": r, "source_file": seg["source_wav"], "score": float(r["score"]),
            "start": start + p2_s, "mid": start + p2_s + (p2_e - p2_s) / 2.0, "other": str(r["phone1"]),
        })

    # Fallback pools if a phone only ever occurred on one side
    for r in rows:
        seg = segments[int(r["line"])]
        start = float(seg["source_start_s"])
        p1, p2 = str(r["phone1"]), str(r["phone2"])
        if not by_left[p2]:
            p2_s, p2_e = float(r["phone2_start_s"]), float(r["phone2_end_s"])
            by_left[p2].append({
                "row": r, "source_file": seg["source_wav"], "score": float(r["score"]),
                "mid": start + p2_s + (p2_e - p2_s) / 2.0, "edge": start + p2_e, "other": p1,
            })
        if not by_right[p1]:
            p1_s, p1_e = float(r["phone1_start_s"]), float(r["phone1_end_s"])
            by_right[p1].append({
                "row": r, "source_file": seg["source_wav"], "score": float(r["score"]),
                "start": start + p1_s, "mid": start + p1_s + (p1_e - p1_s) / 2.0, "other": p2,
            })

    specs: List[Dict[str, object]] = []
    for a in phones:
        for b in phones:
            label = f"{a}_{b}"
            if label in chosen:
                continue
            left = min(by_left[a], key=lambda d: (dist_fn(d["other"], b), -d["score"]))
            right = min(by_right[b], key=lambda d: (dist_fn(d["other"], a), -d["score"]))
            specs.append({
                "label": label, "kind": "fallback", "left": a, "right": b,
                "line": int(left["row"]["line"]), "source_file": left["source_file"],
                "note": f"splice {a}(->{left['other']}) + ({right['other']}->){b}",
                "fallback": {
                    "left_source": left["source_file"],
                    "left_mid": left["mid"], "left_edge": left["edge"], "left_context": left["other"],
                    "right_source": right["source_file"],
                    "right_start": right["start"], "right_mid": right["mid"], "right_context": right["other"],
                },
            })
    return specs


def assemble(spec: Dict[str, object], rate: int, sources: Dict[str, Path]) -> Tuple[np.ndarray, int, int, int]:
    pad_n = int(round(PAD * rate))
    if spec["kind"] in ("natural", "initial", "final") and "splice" not in spec:
        path = sources[str(spec["source_file"])]
        start = float(spec["source_start"])
        begin, middle, end = float(spec["begin"]), float(spec["middle"]), float(spec["end"])
        t0 = start + begin - PAD
        t1 = start + end + PAD
        audio = read_abs_padded(path, rate, t0, t1)
        s0 = int(round(t0 * rate))
        first = int(round((start + begin) * rate)) - s0
        mid_i = int(round((start + middle) * rate)) - s0
        last = int(round((start + end) * rate)) - s0
        if mid_i <= first:
            mid_i = first + 1
        if last <= mid_i:
            last = mid_i + 1
        if not (0 <= first < mid_i < last <= len(audio)):
            raise RuntimeError(f"{spec['label']}: bad indexes {first} {mid_i} {last} of {len(audio)}")
        return audio, first, mid_i, last

    if "splice" in spec:
        sp = spec["splice"]
        path = sources[str(sp["source"])]
        tone_path = sources[str(sp.get("tone_source", sp["source"]))]
        half_start, half_end = float(sp["half_start"]), float(sp["half_end"])
        tone = read_abs_padded(tone_path, rate, float(sp["tone_start"]), float(sp["tone_end"]))
        sil_n = int(round(SILENCE_HALF * rate))
        half_dur = max(2, int(round((half_end - half_start) * rate)))
        if sp["mode"] == "tone_then_half":
            half = read_abs_padded(path, rate, half_start, half_start + (half_dur + pad_n) / float(rate))
            n = min(int(XFADE * rate), max(1, len(tone) // 3), max(1, half_dur // 3))
            audio = np.concatenate([tone[:-n], xfade(tone[-n:], half[:n], n), half[n:]])
            middle = len(tone) - n + n // 2
            first = max(0, middle - sil_n)
            last = min(len(audio) - 1, max(middle + 1, len(tone) - n + half_dur))
        else:
            half = read_abs_padded(path, rate, half_start - pad_n / float(rate), half_start + half_dur / float(rate))
            n = min(int(XFADE * rate), max(1, half_dur // 3), max(1, len(tone) // 3))
            audio = np.concatenate([half[:-n], xfade(half[-n:], tone[:n], n), tone[n:]])
            first = pad_n
            middle = max(first + 1, len(half) - n + n // 2)
            last = min(len(audio) - 1, middle + sil_n)
        if not (0 <= first < middle < last <= len(audio)):
            raise RuntimeError(f"{spec['label']}: bad splice indexes {first} {middle} {last} of {len(audio)}")
        return audio, first, middle, last

    fb = spec["fallback"]
    left_mid = float(fb["left_mid"])
    left_edge = float(fb["left_edge"])
    right_start = float(fb["right_start"])
    right_mid = float(fb["right_mid"])
    left_dur = max(2, int(round((left_edge - left_mid) * rate)))
    right_dur = max(2, int(round((right_mid - right_start) * rate)))
    left = read_abs_padded(
        sources[str(fb["left_source"])],
        rate,
        left_mid - pad_n / float(rate),
        left_mid + left_dur / float(rate),
    )
    right = read_abs_padded(
        sources[str(fb["right_source"])],
        rate,
        right_start,
        right_start + (right_dur + pad_n) / float(rate),
    )
    n = min(int(XFADE * rate), max(1, left_dur // 3), max(1, right_dur // 3))
    if n >= 8:
        audio = np.concatenate([left[:-n], xfade(left[-n:], right[:n], n), right[n:]])
        middle = len(left) - n + n // 2
        last = len(left) - n + right_dur
    else:
        audio = np.concatenate([left, right])
        middle = len(left)
        last = len(left) + right_dur
    first = pad_n
    middle = max(first + 1, middle)
    last = min(len(audio) - 1, max(middle + 1, last))
    if not (0 <= first < middle < last <= len(audio)):
        raise RuntimeError(f"{spec['label']}: bad fallback indexes {first} {middle} {last} of {len(audio)}")
    return audio, first, middle, last


_WORKER: Dict[str, object] = {}


def init_worker(rate: int, sources: Dict[str, str], build_dir: str, name: str, period: int) -> None:
    _WORKER.clear()
    _WORKER.update({
        "rate": rate,
        "sources": {k: Path(v) for k, v in sources.items()},
        "build_dir": Path(build_dir),
        "name": name,
        "period": period,
    })
    _INFO_CACHE.clear()


def process_unit(job: Tuple[int, Dict[str, object]]) -> Dict[str, object]:
    index, spec = job
    rate = int(_WORKER["rate"])
    sources = _WORKER["sources"]
    build_dir = _WORKER["build_dir"]
    name = str(_WORKER["name"])
    period = int(_WORKER["period"])

    audio, first, middle, last = assemble(spec, rate, sources)

    # Guarantee generous outer analysis margin so anaf0/anambe can NEVER read
    # before sample 0 or past len(audio)-1, even after min_span expansion and retries.
    safe_edge = 1024  # > max(f0_win=640, frame_length=960)
    req_margin = 6 * period + safe_edge
    pre_extra = max(0, req_margin - first)
    post_extra = max(0, req_margin - (len(audio) - 1 - last))
    if pre_extra > 0 or post_extra > 0:
        audio = np.concatenate([
            np.zeros(pre_extra, dtype=np.float32),
            audio,
            np.zeros(post_extra, dtype=np.float32),
        ])
        first += pre_extra
        middle += pre_extra
        last += pre_extra

    raw_path = build_dir / "raw" / f"{index:04d}.d16"
    pcm = np.clip(audio, -1.0, 1.0) * 32767.0
    pcm.astype("<i2").tofile(str(raw_path))

    # resynth.exe can segfault on ultra-short spans (< ~7 periods).
    # Ensure a safe minimum analysis span using the surrounding PAD context,
    # and if resynth still fails, retry with wider context while preserving safe_edge.
    min_span = 8 * period
    if (last - first) < min_span:
        need = min_span - (last - first)
        add_l = need // 2
        add_r = need - add_l
        first = max(safe_edge, first - add_l)
        last = min(len(audio) - 1 - safe_edge, last + add_r)

    f0_path = build_dir / "ana" / f"{index}.f0"
    mbe_path = build_dir / "ana" / f"{index}.mbe"
    out_prefix = build_dir / "res" / str(index)

    last_err: Optional[Exception] = None
    for attempt in range(3):
        try:
            run([ANAF0, raw_path, f0_path, first, last, build_dir / f"{name}.f0"])
            run([ANAMBE, raw_path, f0_path, mbe_path, first, last, build_dir / f"{name}.mbe"])
            run([RESYNTH, raw_path, mbe_path, out_prefix, build_dir / f"{name}.syn"])
            last_err = None
            break
        except RuntimeError as exc:
            last_err = exc
            first = max(safe_edge, first - period)
            last = min(len(audio) - 1 - safe_edge, last + period)
    if last_err is not None:
        raise last_err

    s16_path = Path(str(out_prefix) + ".s16")
    pm_path = Path(str(out_prefix) + ".pm")
    for p in (s16_path, pm_path):
        if not p.exists() or p.stat().st_size == 0:
            raise RuntimeError(f"{spec['label']}: resynth did not create {p}")
    return {
        "index": index,
        "label": spec["label"],
        "left": spec["left"],
        "right": spec["right"],
        "kind": spec["kind"],
        "line": spec["line"],
        "source_file": spec["source_file"],
        "note": spec.get("note", ""),
        "first": first,
        "middle": middle,
        "last": last,
        "span_samples": last - first,
        "s16": str(s16_path),
    }


def pad_s16(s16_path: Path, period: int) -> None:
    size = s16_path.stat().st_size
    if size % 2:
        with s16_path.open("r+b") as f:
            f.truncate(size - 1)
    samples = s16_path.stat().st_size // 2
    rem = samples % period
    if rem:
        with s16_path.open("ab") as f:
            f.write(b"\x00" * ((period - rem) * 2))


def peak_of(path: Path) -> int:
    data = array.array("h")
    data.frombytes(path.read_bytes())
    return max((abs(v) for v in data), default=0)


def scale_outputs(paths: Iterable[Path], base_gain: float, peak_limit: float = MAX_PEAK_FRACTION * 32767.0) -> int:
    """Apply uniform `base_gain` across units, capping only rare peak outliers to prevent clipping."""
    capped = 0
    for path in paths:
        data = array.array("h")
        data.frombytes(path.read_bytes())
        pk = max((abs(v) for v in data), default=0)
        g = base_gain
        if pk > 0 and pk * base_gain > peak_limit:
            g = peak_limit / float(pk)
            capped += 1
        path.write_bytes(array.array("h", (max(-32768, min(32767, int(round(v * g)))) for v in data)).tobytes())
    return capped


def validate_db(
    db_path: Path,
    manifest: List[Dict[str, object]],
    out_dir: Path,
    f0_hz: float = 120.0,
) -> List[Dict[str, object]]:
    test_dir = out_dir / "tests"
    test_dir.mkdir(parents=True, exist_ok=True)
    pitch = max(60, min(350, int(round(f0_hz))))
    results: List[Dict[str, object]] = []
    chunk_size = 300
    for off in range(0, len(manifest), chunk_size):
        chunk = manifest[off:off + chunk_size]
        # Never emit back-to-back '_' pauses and always attach a pitch anchor to
        # every non-pause phone so mbrola.exe's 256-phone PhoneBuff never overflows
        # or segfaults (0xC0000005) on large chunks spanning edge (_X / X_) units.
        pho_lines: List[str] = ["_ 60"]

        def _append_pause() -> None:
            if not pho_lines or not pho_lines[-1].startswith("_ "):
                pho_lines.append("_ 60")

        for m in chunk:
            left, right = str(m["left"]), str(m["right"])
            _append_pause()
            if left != "_":
                pho_lines.append(f"{left} 80 50 {pitch}")
            if right != "_":
                pho_lines.append(f"{right} 80 50 {pitch}")
            _append_pause()
        pho = test_dir / f"coverage_{off // chunk_size:02d}.pho"
        wav = pho.with_suffix(".wav")
        pho.write_text("\n".join(pho_lines) + "\n", encoding="ascii")
        run([MBROLA, db_path, pho, wav])
        data, sr = sf.read(str(wav), dtype="int16")
        peak = int(np.max(np.abs(data))) if len(data) else 0
        clipped = int(np.sum(np.abs(data) >= 32767))
        rms = float(np.sqrt(np.mean(data.astype(np.float64) ** 2))) if len(data) else 0.0
        results.append({
            "test": pho.stem, "status": "ok",
            "duration_s": round(len(data) / sr, 2),
            "rms": round(rms, 1), "peak": peak, "clipped": clipped,
        })
        print(f"  {pho.name}: {len(data)/sr:.1f}s rms={rms:.0f} peak={peak} clipped={clipped}", flush=True)
    (out_dir / "validation.json").write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    return results


def write_espeak_voice_files(
    inv: Dict[str, object],
    db_name: str,
    phtrans_name: str,
    rate: int,
    f0_stats: Dict[str, float],
    out_dir: Path,
) -> None:
    """Write faithful eSpeak NG phtrans binary + voice descriptor from inventory.json."""
    voice_code = str(inv["voice"])
    canonical = set(inv["vowels"]) | set(inv["consonants"])
    norm = dict(inv.get("normalization", {}))

    # Standard split percentages matching stock en1_phtrans
    splits = {
        "@L": 50,
        "aI@": 60,
        "aI3": 60,
        "aU@": 75,
        "aU3": 75,
    }

    def pack_name(s: str) -> bytes:
        b = s.encode("ascii")
        if len(b) > 4:
            raise ValueError(f"phone name {s!r} exceeds 4 bytes")
        return b.ljust(4, b"\0")

    # Universal fallback mappings for eSpeak NG markers, rare allophones, and
    # English-switch loanword phonemes that may be emitted at runtime even when
    # absent from a language's recorded canonical inventory. Only rules whose
    # source is NOT in `canonical` and whose target(s) ARE in `canonical` are emitted.
    universal_fallbacks: Dict[str, List[Tuple[str, ...]]] = {
        # Silent / prosodic / glottal markers
        ";": [()], "|": [()], "!": [()], "?": [()], "@-": [()], "*": [()], "g-": [()],
        # Common allophones & length variants
        "E:": [("E2",), ("E",), ("e:",), ("e",)],
        "i:": [("i",), ("I:",), ("I",)],
        "u:": [("u",), ("U",), ("o:",), ("o",)],
        "a:": [("a",), ("A:",), ("A",)],
        "A:": [("a:",), ("A",), ("a",), ("O",)],
        "O:": [("O",), ("o:",), ("o",)],
        "o:": [("o",), ("O:",), ("O",)],
        "e:": [("e",), ("E:",), ("E",)],
        "I:": [("I",), ("i:",), ("i",)],
        "Y:": [("y",), ("8",), ("E2",), ("u",)],
        "a": [("A",), ("a:",), ("E",)],
        "e": [("e:",), ("E",), ("@",)],
        "o": [("o:",), ("O",), ("O:",)],
        "g": [("Q",), ("k",), ("x",)],
        "tS": [("t", "S", "40"), ("S",), ("t",)],
        "dZ": [("d", "Z", "40"), ("d", "j", "50"), ("Z",), ("d",)],
        "t;": [("t", "j", "50"), ("t",)],
        "d;": [("d", "j", "50"), ("d",)],
        "c": [("t", "j", "50"), ("k",)],
        "J": [("d", "j", "50"), ("j",)],
        "s;": [("S",), ("s",)],
        "C": [("x",), ("S",), ("h",)],
        "l/": [("l",)], "l/2": [("l",)], "l-": [("l",)], "L": [("l",)],
        "r/": [("r",)], "r-": [("r",)], "R": [("r",)],
        "w/": [("w",), ("v#",), ("v",)],
        "j/": [("j",)],
        "n^": [("n", "j", "50"), ("n",)],
        "v#": [("w",), ("v",)],
        # English-switch loanword vowels & consonants
        "eI": [("EI",), ("e:",), ("e", "i", "60"), ("E", "i", "60"), ("E",)],
        "aI": [("A", "i", "60"), ("A", "j", "60"), ("a", "i", "60"), ("a", "j", "60"), ("EI",)],
        "OI": [("O", "i", "60"), ("O", "j", "60"), ("o:", "j", "60"), ("O",)],
        "aU": [("VU",), ("A", "u", "60"), ("a", "u", "60"), ("O",)],
        "oU": [("o:",), ("o", "u", "60"), ("o",), ("O",)],
        "i@": [("i", "@", "60"), ("I:",), ("i",)],
        "e@": [("E", "@", "60"), ("E2",), ("E",)],
        "u@": [("u", "@", "60"), ("u",)],
        "U@": [("u", "@", "60"), ("u",)],
        "o@": [("O:",), ("o:",), ("O",)],
        "O@": [("O:",), ("o:",), ("O",)],
        "A@": [("a:",), ("A",), ("a",)],
        "aI@": [("a:", "@", "70"), ("A", "@", "70"), ("a", "@", "70")],
        "aU@": [("VU", "@", "70"), ("A", "@", "70"), ("a", "@", "70")],
        "@L": [("@", "l", "50"), ("l",)],
        "@2": [("@",), ("E",)],
        "@5": [("@",), ("E",)],
        "3": [("@",), ("8",), ("E",)],
        "3:": [("Y:",), ("E2",), ("@",), ("e:",), ("E",)],
        "a#": [("@",), ("a",), ("A",)],
        "V": [("8",), ("@",), ("A",), ("a",)],
        "I2": [("I",), ("i",)],
        "U": [("u",), ("O",), ("o",)],
        "0": [("O",), ("o",), ("A",)],
        "aa": [("a:",), ("A",), ("a",)],
        "T": [("t",), ("s",), ("f",)],
        "D": [("d",), ("z",), ("v",)],
        # French/loanword nasal vowels
        "E~": [("E", "N", "65"), ("E", "n", "65"), ("E",)],
        "A~": [("A", "N", "65"), ("A", "n", "65"), ("A",)],
        "O~": [("O", "N", "65"), ("O", "n", "65"), ("O",)],
        "9~": [("8", "N", "65"), ("@", "n", "65"), ("@",)],
    }

    records = []
    seen_src = set()
    for src, targets in sorted(norm.items()):
        if src in canonical:
            continue
        if len(src.encode("ascii", errors="ignore")) != len(src) or len(src) > 4:
            continue
        if len(targets) > 2:
            continue
        if any(t not in canonical for t in targets):
            continue
        if len(targets) == 2 and src not in splits:
            continue
        out1 = targets[0] if len(targets) >= 1 else ""
        out2 = targets[1] if len(targets) == 2 else ""
        pct = splits.get(src, 0) if len(targets) == 2 else 0
        records.append((src, "", out1, out2, pct, 0))
        seen_src.add(src)

    for src, options in universal_fallbacks.items():
        if src in canonical or src in seen_src:
            continue
        if len(src.encode("ascii", errors="ignore")) != len(src) or len(src) > 4:
            continue
        for opt in options:
            if len(opt) == 0:
                records.append((src, "", "", "", 0, 0))
                seen_src.add(src)
                break
            if len(opt) == 1 and opt[0] in canonical:
                records.append((src, "", opt[0], "", 0, 0))
                seen_src.add(src)
                break
            if len(opt) == 3 and opt[0] in canonical and opt[1] in canonical:
                records.append((src, "", opt[0], opt[1], int(opt[2]), 0))
                seen_src.add(src)
                break

    # Build binary phtrans (header=20 -> 1.25 gain calibrated to volume 0.8)
    buf = bytearray(struct.pack("<I", 20))
    for src, nxt, o1, o2, pct, ctrl in records:
        buf += pack_name(src) + pack_name(nxt) + pack_name(o1) + pack_name(o2)
        buf += struct.pack("<ii", pct, ctrl)

    phtrans_path = out_dir / phtrans_name
    phtrans_path.write_bytes(bytes(buf))

    f0_med = f0_stats["median_hz"]
    pitch_low = max(55, int(round(f0_med * 0.78)))
    pitch_high = max(pitch_low + 25, int(round(f0_med * 1.15)))
    gender = "female" if f0_med >= 165.0 else "male"
    rate_field = f" {rate}" if rate != 16000 else " 16000"

    # In eSpeak NG, the British English phoneme table is named 'en' (not 'en-gb').
    phoneme_table = "en" if voice_code.lower() in ("en-gb", "en-uk") else voice_code
    lang_lines = (
        "language en-gb 3\nlanguage en-uk 3\nlanguage en 2\n"
        if voice_code.lower() in ("en-gb", "en-uk")
        else f"language {voice_code}\n"
    )
    desc = (
        f"name {db_name}\n"
        f"{lang_lines}"
        f"gender {gender}\n"
        f"\n"
        f"phonemes {phoneme_table}\n"
        f"pitch {pitch_low} {pitch_high}\n"
        f"mbrola {db_name} {phtrans_name}{rate_field}\n"
    )
    desc_path = out_dir / f"mb-{db_name}"
    desc_path.write_text(desc, encoding="utf-8")
    print(f"Wrote eSpeak voice files: {phtrans_path} ({len(records)} records) and {desc_path} (pitch {pitch_low}-{pitch_high})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-dir", type=Path, required=True)
    parser.add_argument("--token-meta", type=Path, required=True)
    parser.add_argument("--segments-csv", type=Path, required=True)
    parser.add_argument("--aligned-dir", type=Path, required=True)
    parser.add_argument("--audio-dir", type=Path, default=PROJECT_ROOT / "Voice_audio")
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--name", required=True)
    parser.add_argument("--phtrans-name", default=None)
    parser.add_argument("--rate", type=int, default=16000, choices=[16000, 22050])
    parser.add_argument("--jobs", type=int, default=max(1, min(8, (os.cpu_count() or 4) - 1)))
    parser.add_argument("--period-override", type=int, default=0, help="Optional manual pitch period override (0 = auto from speaker F0)")
    parser.add_argument("--take-overrides", type=Path, default=None, help="Optional JSON mapping diphone -> pinned line number")
    args = parser.parse_args()

    for tool in (ANAF0, ANAMBE, RESYNTH, DATABASE_BUILD, MBROLA):
        if not tool.exists():
            raise SystemExit(f"Missing toolchain binary: {tool}")

    t_start = time.perf_counter()
    inv = json.loads((args.plan_dir / "inventory.json").read_text(encoding="utf-8"))
    dmap_doc = json.loads((args.plan_dir / "diphone_map.json").read_text(encoding="utf-8"))
    dmap_by_line = {int(x["line"]): x for x in dmap_doc["lines"]}
    token_meta = json.loads(args.token_meta.read_text(encoding="utf-8"))
    with args.segments_csv.open("r", encoding="utf-8", newline="") as f:
        segments = {int(r["line"]): r for r in csv.DictReader(f)}

    vowels = list(inv["vowels"])
    consonants = list(inv["consonants"])
    phones = vowels + consonants
    vowel_set = set(vowels)
    ipa_map = dict(inv.get("ipa_map", {}))
    dist_fn = make_distance_fn(vowel_set, ipa_map)

    out_root = args.out_dir
    build_dir = out_root / args.name
    if build_dir.exists():
        shutil.rmtree(build_dir)
    for sub in ("raw", "ana", "res"):
        (build_dir / sub).mkdir(parents=True, exist_ok=True)

    # 1. Build unit grid + estimate speaker F0
    rows, f0_stats = build_unit_grid(
        segments=segments,
        token_meta=token_meta,
        diphone_map=dmap_by_line,
        aligned_dir=args.aligned_dir,
        audio_dir=args.audio_dir,
        vowels=vowel_set,
        out_csv=out_root / "unit_grid.csv",
    )

    # 2. Derive speaker-matched MBE period / frame shift / frame length.
    # anambe.exe crashes (0xC000001D) on odd FrameLength/FrameShift, so always round period to an even integer!
    if args.period_override > 0:
        period = args.period_override + (args.period_override % 2)
    else:
        period = max(40, int(round(args.rate / f0_stats["median_hz"] / 2.0)) * 2)
    frame_shift = period
    frame_length = min(960, 3 * period)
    mbe_params = f"Nfft=1024\nFreq=16000\nFrameLength={frame_length}\nFrameShift={frame_shift}\n"
    print(f"Auto-derived MBROLATOR params at {args.rate} Hz (F0={f0_stats['median_hz']} Hz): period={period}, FrameShift={frame_shift}, FrameLength={frame_length}")

    f0_win = 640 if args.rate > 16000 else 512
    f0_params = f"Freq= 16000\nFrameLength= {f0_win}\nFrameShift= 80\n"
    (build_dir / f"{args.name}.f0").write_text(f0_params, encoding="ascii")
    (build_dir / f"{args.name}.mbe").write_text(mbe_params, encoding="ascii")
    (build_dir / f"{args.name}.syn").write_text(SYN_PARAMS, encoding="ascii")

    # 3. Take selection, edges, fallbacks
    take_overrides: Dict[str, int] = {}
    if args.take_overrides and args.take_overrides.is_file():
        raw_ov = json.loads(args.take_overrides.read_text(encoding="utf-8"))
        take_overrides = {str(k): int(v) for k, v in raw_ov.items()}
        print(f"Loaded {len(take_overrides)} pinned take override(s) from {args.take_overrides}")
    chosen, sel_report = select_takes(rows, overrides=take_overrides)
    with (out_root / "take_selection.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(sel_report[0].keys()))
        w.writeheader()
        w.writerows(sel_report)
    print(f"Selected {len(chosen)} unique recorded diphones from {len(rows)} takes", flush=True)

    sources = prepare_sources(args.rate, build_dir, args.audio_dir, segments)
    naturals = [natural_spec(lbl, r, segments[int(r["line"])]) for lbl, r in sorted(chosen.items())]
    edges = edge_specs(phones, rows, segments, sources, args.rate, build_dir)
    fallbacks = fallback_specs(phones, rows, segments, chosen, dist_fn)
    specs = naturals + edges + fallbacks
    print(f"Matrix units: {len(naturals)} recorded + {len(edges)} edges + {len(fallbacks)} cross-spliced = {len(specs)} total", flush=True)

    # 4. Run MBROLATOR analysis & resynthesis in parallel
    jobs = [(idx, sp) for idx, sp in enumerate(specs)]
    results: List[Dict[str, object]] = []
    failures: List[str] = []
    done = 0
    with ProcessPoolExecutor(
        max_workers=args.jobs,
        initializer=init_worker,
        initargs=(args.rate, {k: str(v) for k, v in sources.items()}, str(build_dir), args.name, period),
    ) as pool:
        futs = {pool.submit(process_unit, j): j for j in jobs}
        for fut in as_completed(futs):
            j = futs[fut]
            try:
                results.append(fut.result())
            except Exception as exc:
                failures.append(f"{j[1]['label']}: {exc}")
                print(f"  FAILED {j[1]['label']}: {exc}", flush=True)
            done += 1
            if done % 50 == 0 or done == len(jobs):
                print(f"  analyzed {done}/{len(jobs)} units", flush=True)

    results.sort(key=lambda x: int(x["index"]))
    if failures:
        (out_root / "build_failures.txt").write_text("\n".join(failures) + "\n", encoding="utf-8")
        print(f"WARNING: {len(failures)} unit failures (see build_failures.txt)", file=sys.stderr)
    if not results:
        raise RuntimeError("All diphone units failed analysis; see build_failures.txt")

    # 5. Pad, gain-scale (using p99 peak so 1-2 isolated plosive spikes cannot drag down
    # the entire voice bank's loudness, while capping rare outliers cleanly below clip), pack database
    s16_paths = [Path(str(item["s16"])) for item in results]
    for p in s16_paths:
        pad_s16(p, period)
    peaks = sorted(peak_of(p) for p in s16_paths)
    max_peak = peaks[-1] if peaks else 0
    p99_peak = peaks[int(0.99 * (len(peaks) - 1))] if peaks else 0
    ref_peak = p99_peak if p99_peak > 0 else max_peak
    gain = RESYNTH_GAIN if ref_peak == 0 else min(RESYNTH_GAIN, MAX_PEAK_FRACTION * 32767.0 / ref_peak)
    capped_units = scale_outputs(s16_paths, gain)
    print(f"Gain={gain:.3f} (p99_peak={p99_peak}, max_peak={max_peak}, headroom_capped={capped_units})", flush=True)

    # database_build.exe reads res/0.s16 .. res/(N-1).s16 by contiguous 0-based .seg line index.
    # If any unit failed, copy surviving .s16/.pm contiguously into res_packed/ so packing cannot
    # crash with "FATAL ERROR input files res/<gap>.s16 !".
    res_arg = "res/"
    if failures:
        pack_dir = build_dir / "res_packed"
        pack_dir.mkdir(parents=True, exist_ok=True)
        for new_idx, item in enumerate(results):
            old_idx = int(item["index"])
            shutil.copy2(build_dir / "res" / f"{old_idx}.s16", pack_dir / f"{new_idx}.s16")
            shutil.copy2(build_dir / "res" / f"{old_idx}.pm", pack_dir / f"{new_idx}.pm")
        res_arg = "res_packed/"

    seg_lines = []
    manifest = []
    for item in results:
        sp = specs[int(item["index"])]
        seg_lines.append(
            f"raw/{int(item['index']):04d}.d16\t{sp['left']}\t{sp['right']}\t"
            f"{item['first']}\t{item['last']}\t{item['middle']}\n"
        )
        manifest.append({
            **{k: v for k, v in item.items() if k != "s16"},
            "fallback": sp.get("fallback"),
            "splice": sp.get("splice"),
        })
    (build_dir / f"{args.name}.seg").write_text("".join(seg_lines), encoding="ascii")
    (build_dir / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

    print("Packing database with database_build.exe...", flush=True)
    res_pack = run(
        [DATABASE_BUILD, args.name, res_arg, res_arg, DATABASE_VERSION, args.rate, period, DATABASE_CODING, DATABASE_VOLUME],
        cwd=build_dir,
    )
    (build_dir / "database_build.log").write_text(res_pack.stdout + res_pack.stderr, encoding="utf-8")
    db_path = build_dir / args.name
    if not db_path.exists() or db_path.stat().st_size == 0:
        raise RuntimeError("database_build did not produce database file")
    print(f"Packed {db_path} ({db_path.stat().st_size / 1e6:.2f} MB)", flush=True)

    # 6. Validate all units through mbrola.exe
    print("Validating all units through mbrola.exe...", flush=True)
    val_results = validate_db(db_path, manifest, out_root, f0_hz=f0_stats["median_hz"])

    # 7. Write faithful eSpeak phtrans & descriptor
    phtrans_name = args.phtrans_name or f"{args.name}_phtrans"
    write_espeak_voice_files(inv, args.name, phtrans_name, args.rate, f0_stats, build_dir)

    # 8. Summary JSON
    summary = {
        "name": args.name,
        "voice": inv["voice"],
        "rate": args.rate,
        "speaker_f0": f0_stats,
        "period": period,
        "frame_shift": frame_shift,
        "frame_length": frame_length,
        "units": len(results),
        "natural": len(naturals),
        "edge": len(edges),
        "fallback": len(fallbacks),
        "failures": len(failures),
        "db_bytes": db_path.stat().st_size,
        "db_mb": round(db_path.stat().st_size / 1e6, 2),
        "gain": round(gain, 4),
        "validation_clipped": sum(v["clipped"] for v in val_results),
        "elapsed_s": round(time.perf_counter() - t_start, 1),
    }
    (out_root / f"build_summary_{args.name}.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print("\n=== BUILD SUMMARY ===")
    print(json.dumps(summary, indent=2))
    return 0 if not failures else 2


if __name__ == "__main__":
    sys.exit(main())
