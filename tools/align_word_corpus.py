#!/usr/bin/env python3
"""Reusable forced-alignment, VAD integrity-check, and clip-slicing stage for word corpora.

Bridges `tools/plan_word_corpus.py` and `tools/build_word_mbrola_db.py`, supporting
both external-recorder long WAVs (with strict range-map validation) and direct
per-line recordings.

Subcommands:
  prepare   Build MFA dictionary + token metadata + whole-file alignment inputs
            from a plan directory (`inventory.json`, `diphone_map.json`) and
            either a single WAV or a strictly validated `ranges.txt` file.
  align1    Run Pass-1 whole-file MFA alignment on the prepared corpus.
  verify    Cross-check Pass-1 MFA word alignments against acoustic speech
            islands (VAD) to detect skipped lines, retakes, or silence leaks.
  slice     Trim Pass-1 outer silence leaks and cut clean 44.1 kHz per-line
            clips into `segments/` with `segments.csv` (`source_start_s`
            preserving exact offsets into read-only originals), plus Pass-2
            corpus inputs.
  align2    Run Pass-2 per-line MFA alignment on the sliced clips.

All audio sources under `--audio-dir` (e.g. `Voice_audio/`) are read-only.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import statistics
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np
import soundfile as sf
from scipy.signal import resample_poly

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
DEFAULT_MFA_ROOT = Path(r"C:\diphone-mfa-env")

sys.path.insert(0, str(SCRIPT_DIR))
from resegment_from_whole_alignment import parse_textgrid  # noqa: E402

# Verified english_mfa v3.1.0 phone set mapping for British/multi-accent English.
# Other languages fall back directly to inventory.json["ipa_map"] (or a custom
# --phone-map JSON file), verified against the target MFA model's phone inventory.
ENGLISH_MFA_PHONE_MAP: Dict[str, List[str]] = {
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
    "l": ["l"], "r": ["ɹ"], "w": ["w"], "j": ["j"],
}

ENGLISH_MFA_VARIANTS: Dict[str, str] = {
    "pʰ": "p", "tʰ": "t", "kʰ": "k", "ɫ": "l", "a": "æ", "oː": "ɔ", "əw": "ow",
}


def mfa_env(mfa_root: Path) -> Dict[str, str]:
    env = os.environ.copy()
    prepend = os.pathsep.join([
        str(mfa_root),
        str(mfa_root / "Scripts"),
        str(mfa_root / "Library" / "bin"),
    ])
    env["PATH"] = prepend + os.pathsep + env.get("PATH", "")
    return env


def run_mfa(
    mfa_root: Path,
    corpus_dir: Path,
    dict_path: Path,
    model: str,
    out_dir: Path,
    num_jobs: int = 1,
    beam: int = 100,
    retry_beam: int = 400,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(mfa_root / "python.exe"),
        "-m",
        "montreal_forced_aligner.command_line.mfa",
        "align",
        str(corpus_dir),
        str(dict_path),
        model,
        str(out_dir),
        "--clean",
        "--single_speaker",
        "--num_jobs", str(num_jobs),
        "--beam", str(beam),
        "--retry_beam", str(retry_beam),
    ]
    print("Running MFA:", " ".join(cmd), flush=True)
    subprocess.run(cmd, env=mfa_env(mfa_root), check=True)


def parse_and_validate_ranges(
    ranges_path: Optional[Path],
    single_wav: Optional[Path],
    audio_dir: Path,
    expected_lines: List[int],
) -> List[Tuple[str, int, int]]:
    """Strictly validate that every prompt line maps to exactly one recording range.

    Invariant: every prompt line appears once, with no gaps, no overlaps, and
    every referenced WAV file exists on disk.
    """
    expected_set = set(expected_lines)
    if single_wav is not None:
        wav_name = single_wav.name
        full = single_wav if single_wav.exists() else (audio_dir / wav_name)
        if not full.exists():
            raise SystemExit(f"Source WAV not found: {full}")
        return [(wav_name, min(expected_lines), max(expected_lines))]

    if ranges_path is None or not ranges_path.exists():
        raise SystemExit("Provide either --wav <file> or --ranges <ranges.txt>")

    entries: List[Tuple[str, int, int]] = []
    seen_lines: Dict[int, str] = {}
    for raw in ranges_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 3:
            raise SystemExit(f"Invalid range line (expected 'WAV first last'): {line!r}")
        wav_name, lo_s, hi_s = parts
        lo, hi = int(lo_s), int(hi_s)
        if lo > hi:
            raise SystemExit(f"Invalid range {lo}..{hi} in {wav_name}")
        if not (audio_dir / wav_name).exists():
            raise SystemExit(f"Missing source WAV listed in ranges: {audio_dir / wav_name}")
        for ln in range(lo, hi + 1):
            if ln in seen_lines:
                raise SystemExit(f"Overlap on prompt line {ln}: in both {seen_lines[ln]} and {wav_name}")
            seen_lines[ln] = wav_name
        entries.append((wav_name, lo, hi))

    missing = sorted(expected_set - set(seen_lines))
    extra = sorted(set(seen_lines) - expected_set)
    if missing:
        raise SystemExit(f"Range map is missing {len(missing)} prompt line(s): {missing[:15]}")
    if extra:
        raise SystemExit(f"Range map has {len(extra)} out-of-range line(s): {extra[:15]}")
    return entries


def load_phone_map(inv: Dict[str, object], model: str, custom_map: Optional[Path]) -> Dict[str, List[str]]:
    if custom_map is not None:
        raw = json.loads(custom_map.read_text(encoding="utf-8"))
        return {k: (v if isinstance(v, list) else [v]) for k, v in raw.items()}
    if model == "english_mfa":
        return dict(ENGLISH_MFA_PHONE_MAP)
    # Generic language-neutral fallback: use discovered IPA directly
    ipa_map = dict(inv.get("ipa_map", {}))
    return {ph: [str(ipa_map.get(ph, ph))] for ph in list(inv["vowels"]) + list(inv["consonants"])}


def cmd_prepare(args: argparse.Namespace) -> None:
    inv = json.loads((args.plan_dir / "inventory.json").read_text(encoding="utf-8"))
    dmap = json.loads((args.plan_dir / "diphone_map.json").read_text(encoding="utf-8"))
    lines = dmap["lines"]
    by_line = {int(x["line"]): x for x in lines}
    vowels = set(inv["vowels"])

    ranges = parse_and_validate_ranges(args.ranges, args.wav, args.audio_dir, sorted(by_line))
    phone_map = load_phone_map(inv, args.model, args.phone_map)

    out_dir: Path = args.align_dir
    whole_dir = out_dir / "whole_corpus"
    whole_dir.mkdir(parents=True, exist_ok=True)

    dict_lines: List[str] = []
    token_meta: Dict[str, Dict[str, object]] = {}
    range_manifest = []

    for wav_name, lo, hi in ranges:
        src_path = (args.wav if (args.wav and args.wav.exists() and args.wav.name == wav_name) else args.audio_dir / wav_name)
        stem = Path(wav_name).stem
        out_wav = whole_dir / f"{stem}.wav"
        if not out_wav.exists() or args.force:
            audio, sr = sf.read(str(src_path), dtype="float32", always_2d=True)
            mono = audio[:, 0] if audio.shape[1] == 1 else audio.mean(axis=1)
            g = math.gcd(16000, sr)
            mono16 = resample_poly(mono, 16000 // g, sr // g).astype(np.float32) if sr != 16000 else mono
            sf.write(str(out_wav), mono16, 16000, subtype="PCM_16")

        wav_tokens = []
        for ln in range(lo, hi + 1):
            item = by_line[ln]
            kind = str(item.get("kind", "word"))
            tok = f"l{ln:04d}_{item['text']}" if kind == "word" else f"l{ln:04d}_sup"
            wav_tokens.append(tok)

            phones = list(item["phones"])
            mfa_seq: List[str] = []
            spans: List[Tuple[str, int]] = []
            for idx, ph in enumerate(phones):
                seq = list(phone_map[ph])
                if args.model == "english_mfa":
                    if idx == 0 and ph in ("p", "t", "k") and len(phones) > 1 and phones[1] in vowels:
                        seq = [ph + "ʰ"]
                    if ph == "l" and (idx == len(phones) - 1 or phones[idx + 1] not in vowels):
                        seq = ["ɫ"]
                mfa_seq.extend(seq)
                spans.append((ph, len(seq)))

            dict_lines.append(f"{tok}\t{' '.join(mfa_seq)}")
            if args.model == "english_mfa":
                alt = [ENGLISH_MFA_VARIANTS.get(p, p) for p in mfa_seq]
                if alt != mfa_seq:
                    dict_lines.append(f"{tok}\t{' '.join(alt)}")

            token_meta[tok] = {
                "line": ln,
                "kind": kind,
                "text": str(item["text"]),
                "phones": phones,
                "spans": spans,
                "source_wav": wav_name,
            }

        (whole_dir / f"{stem}.lab").write_text(" ".join(wav_tokens) + "\n", encoding="utf-8")
        range_manifest.append({"source_wav": wav_name, "stem": stem, "first_line": lo, "last_line": hi})

    (out_dir / "mfa_dict.txt").write_text("\n".join(dict_lines) + "\n", encoding="utf-8")
    (out_dir / "token_meta.json").write_text(json.dumps(token_meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    (out_dir / "ranges_validated.json").write_text(json.dumps(range_manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Prepared {len(token_meta)} tokens across {len(ranges)} WAV(s) in {out_dir}")


def compute_frame_db(mono: np.ndarray, sr: int) -> Tuple[np.ndarray, float]:
    hop = int(sr * 0.010)
    win = int(sr * 0.020)
    n_frames = max(1, (len(mono) - win) // hop + 1)
    cs = np.concatenate(([0.0], np.cumsum(mono.astype(np.float64) ** 2)))
    idx = np.arange(n_frames) * hop
    rms = np.sqrt(np.maximum(0.0, (cs[idx + win] - cs[idx]) / win))
    fdb = 20.0 * np.log10(np.maximum(rms, 1e-12))
    return fdb, float(np.percentile(fdb, 10))


def cmd_verify(args: argparse.Namespace) -> None:
    out_dir: Path = args.align_dir
    meta = json.loads((out_dir / "token_meta.json").read_text(encoding="utf-8"))
    ranges = json.loads((out_dir / "ranges_validated.json").read_text(encoding="utf-8"))

    total_aligned = 0
    total_phone_ok = 0
    total_flags = 0

    for rec in ranges:
        stem = rec["stem"]
        tg_path = out_dir / "aligned_whole" / f"{stem}.TextGrid"
        wav_path = out_dir / "whole_corpus" / f"{stem}.wav"
        if not tg_path.exists():
            raise SystemExit(f"Missing Pass-1 TextGrid: {tg_path}")

        tiers = parse_textgrid(tg_path)
        word_ivs = [iv for iv in tiers.get("words", []) if iv["text"]]
        phone_ivs = [iv for iv in tiers.get("phones", []) if iv["text"] and iv["text"] not in ("sil", "sp", "spn")]

        audio, sr = sf.read(str(wav_path), dtype="float32")
        frame_db, noise_p10 = compute_frame_db(audio, sr)

        def seg_rms(t0: float, t1: float) -> float:
            i0, i1 = max(0, int(t0 * sr)), min(len(audio), int(t1 * sr))
            if i1 <= i0:
                return -120.0
            return 20.0 * math.log10(max(float(np.sqrt(np.mean(audio[i0:i1].astype(np.float64) ** 2))), 1e-12))

        expected_n_words = rec["last_line"] - rec["first_line"] + 1
        print(f"\n=== {rec['source_wav']} (lines {rec['first_line']}..{rec['last_line']}) ===")
        print(f"Aligned words: {len(word_ivs)} / {expected_n_words} | Noise floor: {noise_p10:.1f} dBFS")
        total_aligned += len(word_ivs)

        flags = []
        for i, wiv in enumerate(word_ivs):
            tok = str(wiv["text"])
            m = meta.get(tok)
            if not m:
                flags.append((0, tok, float(wiv["xmin"]), float(wiv["xmax"]), "UNKNOWN_TOKEN"))
                continue
            t0, t1 = float(wiv["xmin"]), float(wiv["xmax"])
            dur = t1 - t0
            rms = seg_rms(t0, t1)
            w_phones = [
                p for p in phone_ivs
                if (float(p["xmin"]) + float(p["xmax"])) * 0.5 >= t0 - 1e-4
                and (float(p["xmin"]) + float(p["xmax"])) * 0.5 <= t1 + 1e-4
            ]
            exp_p = sum(int(c) for _, c in m["spans"])
            if len(w_phones) == exp_p:
                total_phone_ok += 1
            reasons = []
            if len(w_phones) != exp_p:
                reasons.append(f"PHONE_MISMATCH({len(w_phones)}!={exp_p})")
            if rms < noise_p10 + 12.0:
                reasons.append(f"LOW_ENERGY({rms:.1f}dB)")
            max_pd = max(((float(p["xmax"]) - float(p["xmin"])) * 1000.0 for p in w_phones), default=0.0)
            if max_pd > 550.0:
                reasons.append(f"SILENCE_LEAK({max_pd:.0f}ms)")
            if dur < 0.14:
                reasons.append(f"VERY_SHORT({dur:.2f}s)")
            if reasons:
                flags.append((m["line"], m["text"], t0, t1, ", ".join(reasons)))

        total_flags += len(flags)
        if flags:
            print(f"Flagged lines ({len(flags)}):")
            for ln, txt, t0, t1, rsn in flags[:25]:
                print(f"  line {ln:04d} ({txt:16s}) [{t0:7.2f}s .. {t1:7.2f}s]: {rsn}")
        else:
            print("All lines passed with zero flags.")

    print(f"\nTotal verified: {total_aligned}/{len(meta)} words, {total_phone_ok}/{len(meta)} exact phone counts, {total_flags} flags.")


def cmd_slice(args: argparse.Namespace) -> None:
    out_dir: Path = args.align_dir
    build_dir: Path = args.build_dir
    seg_dir = build_dir / "segments"
    mfa_p2 = build_dir / "mfa_pass2_corpus"
    seg_dir.mkdir(parents=True, exist_ok=True)
    mfa_p2.mkdir(parents=True, exist_ok=True)

    meta = json.loads((out_dir / "token_meta.json").read_text(encoding="utf-8"))
    ranges = json.loads((out_dir / "ranges_validated.json").read_text(encoding="utf-8"))
    rows: List[Dict[str, object]] = []

    for rec in ranges:
        stem = rec["stem"]
        wav_name = rec["source_wav"]
        src_path = args.audio_dir / wav_name
        tg_path = out_dir / "aligned_whole" / f"{stem}.TextGrid"

        tiers = parse_textgrid(tg_path)
        word_ivs = [iv for iv in tiers.get("words", []) if iv["text"]]
        audio, sr = sf.read(str(src_path), dtype="float32", always_2d=True)
        mono = audio[:, 0] if audio.shape[1] == 1 else audio.mean(axis=1)
        total_dur = len(mono) / sr
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
            rs, re_ = float(wiv["xmin"]), float(wiv["xmax"])
            ts, te = trim_outer(rs, re_)
            trimmed.append((tok, rs, re_, ts, te))

        pad_s = args.pad_s
        for i, (tok, rs, re_, ts, te) in enumerate(trimmed):
            m = meta[tok]
            ln = int(m["line"])
            prev_end = trimmed[i - 1][4] if i > 0 else 0.0
            next_start = trimmed[i + 1][3] if i + 1 < len(trimmed) else total_dur
            clip_s = max(0.0, ts - pad_s, 0.5 * (prev_end + ts) if i > 0 else 0.0)
            clip_e = min(total_dur, te + pad_s, 0.5 * (te + next_start) if i + 1 < len(trimmed) else total_dur)

            s_idx = int(round(clip_s * sr))
            e_idx = int(round(clip_e * sr))
            clip = mono[s_idx:e_idx]
            fname = f"line_{ln:04d}.wav"
            sf.write(str(seg_dir / fname), clip, sr, subtype="PCM_16")
            sf.write(str(mfa_p2 / fname), clip, sr, subtype="PCM_16")
            (mfa_p2 / f"line_{ln:04d}.lab").write_text(f"{tok}\n", encoding="utf-8")

            rows.append({
                "line": ln,
                "token": tok,
                "kind": m["kind"],
                "text": m["text"],
                "source_wav": wav_name,
                "source_start_s": f"{s_idx / sr:.6f}",
                "source_end_s": f"{e_idx / sr:.6f}",
                "duration_s": f"{(e_idx - s_idx) / sr:.6f}",
                "pass1_raw_dur_s": f"{re_ - rs:.4f}",
                "trimmed_dur_s": f"{te - ts:.4f}",
                "segment_wav": fname,
            })

    rows.sort(key=lambda r: int(r["line"]))
    seg_csv = seg_dir / "segments.csv"
    with seg_csv.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"Sliced {len(rows)} clean per-line clips into {seg_dir} (segments.csv written)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_prep = sub.add_parser("prepare", help="Prepare Pass-1 whole-file corpus and MFA dictionary.")
    p_prep.add_argument("--plan-dir", type=Path, required=True)
    p_prep.add_argument("--align-dir", type=Path, required=True)
    p_prep.add_argument("--audio-dir", type=Path, default=PROJECT_ROOT / "Voice_audio")
    p_prep.add_argument("--wav", type=Path, default=None, help="Single recording WAV covering all prompts.")
    p_prep.add_argument("--ranges", type=Path, default=None, help="Multi-WAV range map ('WAV first last').")
    p_prep.add_argument("--model", default="english_mfa", help="MFA acoustic model name.")
    p_prep.add_argument("--phone-map", type=Path, default=None, help="Optional JSON mapping eSpeak phone -> MFA phone(s).")
    p_prep.add_argument("--force", action="store_true")

    p_a1 = sub.add_parser("align1", help="Run Pass-1 whole-file MFA alignment.")
    p_a1.add_argument("--align-dir", type=Path, required=True)
    p_a1.add_argument("--model", default="english_mfa")
    p_a1.add_argument("--mfa-root", type=Path, default=DEFAULT_MFA_ROOT)
    p_a1.add_argument("--jobs", type=int, default=1)

    p_ver = sub.add_parser("verify", help="Cross-check Pass-1 MFA alignments against acoustic energy.")
    p_ver.add_argument("--align-dir", type=Path, required=True)

    p_sl = sub.add_parser("slice", help="Trim outer silence leaks and cut per-line clips + segments.csv.")
    p_sl.add_argument("--align-dir", type=Path, required=True)
    p_sl.add_argument("--build-dir", type=Path, required=True)
    p_sl.add_argument("--audio-dir", type=Path, default=PROJECT_ROOT / "Voice_audio")
    p_sl.add_argument("--pad-s", type=float, default=0.14)

    p_a2 = sub.add_parser("align2", help="Run Pass-2 per-line MFA alignment on sliced clips.")
    p_a2.add_argument("--align-dir", type=Path, required=True)
    p_a2.add_argument("--build-dir", type=Path, required=True)
    p_a2.add_argument("--model", default="english_mfa")
    p_a2.add_argument("--mfa-root", type=Path, default=DEFAULT_MFA_ROOT)
    p_a2.add_argument("--jobs", type=int, default=4)

    args = parser.parse_args()
    if args.cmd == "prepare":
        cmd_prepare(args)
    elif args.cmd == "align1":
        run_mfa(args.mfa_root, args.align_dir / "whole_corpus", args.align_dir / "mfa_dict.txt",
                args.model, args.align_dir / "aligned_whole", num_jobs=args.jobs, beam=100, retry_beam=400)
    elif args.cmd == "verify":
        cmd_verify(args)
    elif args.cmd == "slice":
        cmd_slice(args)
    elif args.cmd == "align2":
        run_mfa(args.mfa_root, args.build_dir / "mfa_pass2_corpus", args.align_dir / "mfa_dict.txt",
                args.model, args.build_dir / "mfa_aligned", num_jobs=args.jobs, beam=40, retry_beam=160)


if __name__ == "__main__":
    main()
