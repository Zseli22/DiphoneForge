#!/usr/bin/env python3
"""Build a tiny MBROLA database from the existing MFA alignments.

This is the step-2 proof of concept: it converts reviewed phone boundaries
from output/unit_grid/unit_grid.csv into MBROLATOR .seg records, extracts
audio with context from the original recordings, runs the MBROLATOR analysis
chain, and packs a database at 16 kHz and 22.05 kHz.

The original recordings under Voice_audio/ are read only.
"""

from __future__ import annotations

import argparse
import array
import csv
import json
import math
import shutil
import subprocess
import sys
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]

UNIT_GRID = PROJECT_ROOT / "output" / "unit_grid" / "unit_grid.csv"
SEGMENTS = PROJECT_ROOT / "output" / "segments" / "segments.csv"
VOICE_AUDIO = PROJECT_ROOT / "Voice_audio"

ANAF0 = PROJECT_ROOT / "tools" / "mbrolator-src" / "AnaMBE" / "Obj" / "anaf0.exe"
ANAMBE = PROJECT_ROOT / "tools" / "mbrolator-src" / "AnaMBE" / "Obj" / "anambe.exe"
RESYNTH = PROJECT_ROOT / "tools" / "mbrolator-src" / "Resynthesis" / "resynth.exe"
DATABASE_BUILD = PROJECT_ROOT / "tools" / "mbrolator-src" / "Resynthesis" / "database_build.exe"
MBROLA = PROJECT_ROOT / "tools" / "mbrola-src" / "Bin" / "mbrola.exe"

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
DATABASE_VOLUME = 1.0
RESYNTH_GAIN = 3.5


@dataclass
class UnitSpec:
    diphone: str
    kind: str  # regular | initial | final
    line: int
    phone: Optional[str] = None
    donor: Optional[Dict[str, str]] = None
    source_file: str = ""
    source_start: float = 0.0
    begin: float = 0.0
    middle: float = 0.0
    end: float = 0.0
    row: Dict[str, str] = field(default_factory=dict)
    left: str = ""
    right: str = ""


@dataclass(frozen=True)
class RateConfig:
    """Per-rate MBROLATOR analysis and packing parameters.

    MBROLATOR's analysis programs are built around Freq=16000.  For a
    22.05 kHz database the sources are analyzed with Freq=16000, but the
    MBE frame length and shift are expressed in samples of the real audio
    rate, so they must cover the speaker's pitch period at that rate.  The
    real rate is passed only to database_build.
    """

    rate: int
    mbe_params: str
    period: int


# The speaker's median F0 is about 122 Hz:
#   16000 / 122 = 131 samples (the POC keeps its proven 120)
#   22050 / 122 = 181 samples (180 keeps the period even)
RATE_CONFIGS: Dict[int, RateConfig] = {
    16000: RateConfig(
        rate=16000,
        mbe_params="""Nfft=1024
Freq=16000
FrameLength=360
FrameShift=120
""",
        period=120,
    ),
    22050: RateConfig(
        rate=22050,
        mbe_params="""Nfft=1024
Freq=16000
FrameLength=540
FrameShift=180
""",
        period=180,
    ),
}


def run(cmd: List[str], cwd: Optional[Path] = None, quiet: bool = False) -> subprocess.CompletedProcess:
    if not quiet:
        print("  $ " + " ".join(str(c) for c in cmd))
    result = subprocess.run(
        [str(c) for c in cmd],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(str(c) for c in cmd)}\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def load_csv(path: Path) -> List[Dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def index_rows(rows: List[Dict[str, str]]) -> Dict[Tuple[int, str], Dict[str, str]]:
    result: Dict[Tuple[int, str], Dict[str, str]] = {}
    for row in rows:
        key = (int(row["line"]), row["diphone"])
        result.setdefault(key, row)
    return result


def ffprobe_duration(path: Path) -> float:
    result = run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        quiet=True,
    )
    return float(result.stdout.strip())


def build_units(
    unit_rows: List[Dict[str, str]],
    segments: Dict[int, Dict[str, str]],
    silence_half: float,
) -> List[UnitSpec]:
    rows = index_rows(unit_rows)
    specs = [
        UnitSpec("_s", "initial", 277, "s"),
        UnitSpec("s_i:", "regular", 277),
        UnitSpec("i:_", "final", 277, "i:"),
        UnitSpec("_t", "initial", 27, "t"),
        UnitSpec("t_a", "regular", 27),
        UnitSpec("a_p", "regular", 591),
        UnitSpec("p_", "final", 591, "p"),
        UnitSpec("_p", "initial", 4, "p"),
        UnitSpec("p_a", "regular", 4),
        UnitSpec("a_t", "regular", 592),
        UnitSpec("t_", "final", 715, "t"),
    ]

    for spec in specs:
        if spec.kind == "regular":
            spec.left, spec.right = spec.diphone.split("_", 1)
        elif spec.kind == "initial":
            spec.left, spec.right = "_", spec.phone or ""
        else:
            spec.left, spec.right = spec.phone or "", "_"

        segment = segments[spec.line]
        spec.source_file = segment["source_file"]
        spec.source_start = float(segment["source_start_s"])

        row = rows.get((spec.line, spec.diphone))
        if row is not None and spec.kind == "regular":
            spec.row = row
            p1_start, p1_end = float(row["phone1_start_s"]), float(row["phone1_end_s"])
            p2_start, p2_end = float(row["phone2_start_s"]), float(row["phone2_end_s"])
            spec.begin = p1_start + (p1_end - p1_start) / 2.0
            spec.middle = p1_end if abs(p1_end - p2_start) < 1e-4 else p2_start
            spec.end = p2_start + (p2_end - p2_start) / 2.0
            continue

        if spec.phone is None:
            raise ValueError(f"{spec.diphone}: missing phone for boundary unit")

        donor = None
        for candidate in unit_rows:
            left, right = candidate["diphone"].split("_", 1)
            if spec.kind == "initial":
                if int(candidate["line"]) != spec.line or left != spec.phone:
                    continue
                if abs(float(candidate["phone1_start_s"]) - float(candidate["word_start_s"])) < 1e-4:
                    donor = candidate
                    break
            else:
                if int(candidate["line"]) != spec.line or right != spec.phone:
                    continue
                if abs(float(candidate["phone2_end_s"]) - float(candidate["word_end_s"])) < 1e-4:
                    donor = candidate
                    break
        if donor is None:
            raise ValueError(f"{spec.diphone}: no word-edge donor on line {spec.line}")

        spec.row = donor
        spec.donor = donor
        if spec.kind == "initial":
            word_edge = float(donor["word_start_s"])
            phone_start = float(donor["phone1_start_s"])
            phone_end = float(donor["phone1_end_s"])
            spec.begin = word_edge - silence_half
            spec.middle = word_edge
            spec.end = phone_start + (phone_end - phone_start) / 2.0
        else:
            word_edge = float(donor["word_end_s"])
            phone_start = float(donor["phone2_start_s"])
            phone_end = float(donor["phone2_end_s"])
            spec.begin = phone_start + (phone_end - phone_start) / 2.0
            spec.middle = word_edge
            spec.end = word_edge + silence_half

        if not (spec.begin < spec.middle < spec.end):
            raise ValueError(f"{spec.diphone}: invalid span {spec.begin} {spec.middle} {spec.end}")

    return specs


def sanitize(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-." else "-" for c in name)


def extract_audio(
    spec: UnitSpec,
    index: int,
    rate: int,
    pad: float,
    out_dir: Path,
    duration_cache: Dict[str, float],
) -> Tuple[Path, List[int]]:
    source = VOICE_AUDIO / spec.source_file
    if spec.source_file not in duration_cache:
        duration_cache[spec.source_file] = ffprobe_duration(source)
    duration = duration_cache[spec.source_file]

    wanted_start = spec.source_start + spec.begin - pad
    wanted_end = spec.source_start + spec.end + pad
    actual_start = max(0.0, wanted_start)
    actual_end = min(duration, wanted_end)
    if actual_end - actual_start <= 0.01:
        raise ValueError(f"{spec.diphone}: empty extraction window")

    out_path = out_dir / f"{index:02d}_{sanitize(spec.diphone)}.d16"
    command = [
        "ffmpeg",
        "-nostdin",
        "-v",
        "error",
        "-ss",
        f"{actual_start:.6f}",
        "-i",
        str(source),
        "-t",
        f"{actual_end - actual_start:.6f}",
        "-ac",
        "1",
        "-ar",
        str(rate),
        "-af",
        "aresample=resampler=soxr:precision=28",
        "-acodec",
        "pcm_s16le",
        "-f",
        "s16le",
        "-y",
        str(out_path),
    ]
    run(command, quiet=True)

    samples = out_path.stat().st_size // 2
    first = round((spec.source_start + spec.begin - actual_start) * rate)
    middle = round((spec.source_start + spec.middle - actual_start) * rate)
    last = round((spec.source_start + spec.end - actual_start) * rate)
    if not (0 <= first < middle < last <= samples):
        raise ValueError(
            f"{spec.diphone}: sample indices {first} {middle} {last} outside {samples}"
        )
    # Keep an analysis margin at both ends of the raw file.
    if first < 0.05 * rate or samples - last < 0.05 * rate:
        print(f"  warning: {spec.diphone}: thin analysis margin ({first}, {samples - last})")
    return out_path, [first, middle, last]


def run_build(
    rate: int,
    specs: List[UnitSpec],
    out_root: Path,
    pad: float,
) -> Path:
    if rate not in RATE_CONFIGS:
        raise ValueError(f"no rate configuration for {rate} Hz")
    config = RATE_CONFIGS[rate]

    name = f"poc{rate // 1000}" if rate % 1000 == 0 else f"poc{rate}"
    build_dir = out_root / name
    raw_dir = build_dir / "raw"
    ana_dir = build_dir / "ana"
    res_dir = build_dir / "res"
    if build_dir.exists():
        shutil.rmtree(build_dir)
    for directory in (raw_dir, ana_dir, res_dir):
        directory.mkdir(parents=True, exist_ok=True)

    (build_dir / f"{name}.f0").write_text(F0_PARAMS, encoding="ascii")
    (build_dir / f"{name}.mbe").write_text(config.mbe_params, encoding="ascii")
    (build_dir / f"{name}.syn").write_text(SYN_PARAMS, encoding="ascii")

    seg_lines = []
    manifest = []
    duration_cache: Dict[str, float] = {}
    timings = {
        "extract": 0.0,
        "anaf0": 0.0,
        "anambe": 0.0,
        "resynth": 0.0,
        "pack": 0.0,
    }
    build_start = time.perf_counter()

    for index, spec in enumerate(specs):
        print(f"[{name}] {index:02d} {spec.diphone} line {spec.line}")
        extract_start = time.perf_counter()
        raw_path, indices = extract_audio(spec, index, rate, pad, raw_dir, duration_cache)
        timings["extract"] += time.perf_counter() - extract_start
        first, middle, last = indices
        seg_lines.append(
            f"{raw_path.name}\t{spec.left}\t{spec.right}\t{first}\t{last}\t{middle}\n"
        )
        manifest.append(
            {
                "diphone": spec.diphone,
                "kind": spec.kind,
                "line": spec.line,
                "source_file": spec.source_file,
                "audio": str(raw_path.relative_to(build_dir)),
                "rate": rate,
                "first": first,
                "middle": middle,
                "last": last,
                "begin_s": spec.begin,
                "middle_s": spec.middle,
                "end_s": spec.end,
            }
        )

    (build_dir / f"{name}.seg").write_text("".join(seg_lines), encoding="ascii")
    (build_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print(f"[{name}] MBE period={config.period} samples at {rate} Hz")

    for index, spec in enumerate(specs):
        raw_path = raw_dir / f"{index:02d}_{sanitize(spec.diphone)}.d16"
        first, last = manifest[index]["first"], manifest[index]["last"]
        f0_path = ana_dir / f"{index}.f0"
        mbe_path = ana_dir / f"{index}.mbe"
        out_prefix = res_dir / str(index)
        print(f"[{name}] analysis {index:02d} {spec.diphone}")

        anaf0_start = time.perf_counter()
        run([ANAF0, raw_path, f0_path, first, last, build_dir / f"{name}.f0"], quiet=False)
        anaf0_time = time.perf_counter() - anaf0_start

        anambe_start = time.perf_counter()
        run([ANAMBE, raw_path, f0_path, mbe_path, first, last, build_dir / f"{name}.mbe"], quiet=False)
        anambe_time = time.perf_counter() - anambe_start

        resynth_start = time.perf_counter()
        run([RESYNTH, raw_path, mbe_path, out_prefix, build_dir / f"{name}.syn"], quiet=False)
        resynth_time = time.perf_counter() - resynth_start

        timings["anaf0"] += anaf0_time
        timings["anambe"] += anambe_time
        timings["resynth"] += resynth_time

        s16_path = Path(str(out_prefix) + ".s16")
        pm_path = Path(str(out_prefix) + ".pm")
        pad_resynth_output(s16_path, config.period)
        scale_resynth_output(s16_path, RESYNTH_GAIN)
        for suffix in (".s16", ".pm"):
            path = Path(str(out_prefix) + suffix)
            if not path.exists() or path.stat().st_size == 0:
                raise RuntimeError(f"{spec.diphone}: resynth did not create {path}")

        print(
            f"[{name}] analysis {index:02d} {spec.diphone}: "
            f"anaf0={anaf0_time:.2f}s anambe={anambe_time:.2f}s resynth={resynth_time:.2f}s"
        )

    print(f"[{name}] packing database (rate={rate}, period={config.period})")
    pack_start = time.perf_counter()
    result = run(
        [
            DATABASE_BUILD,
            name,
            "res/",
            "res/",
            DATABASE_VERSION,
            rate,
            config.period,
            DATABASE_CODING,
            DATABASE_VOLUME,
        ],
        cwd=build_dir,
    )
    timings["pack"] = time.perf_counter() - pack_start
    print(result.stdout)
    db_path = build_dir / name
    if not db_path.exists() or db_path.stat().st_size == 0:
        raise RuntimeError(f"database_build did not create {db_path}")

    total = time.perf_counter() - build_start
    print(
        f"[{name}] timing: total={total:.1f}s "
        f"extract={timings['extract']:.1f}s "
        f"anaf0={timings['anaf0']:.1f}s "
        f"anambe={timings['anambe']:.1f}s "
        f"resynth={timings['resynth']:.1f}s "
        f"pack={timings['pack']:.1f}s"
    )
    return db_path


def pad_resynth_output(s16_path: Path, period: int) -> None:
    """Pad the resynthesized waveform to a whole number of MBR frames.

    database_build treats a partial final frame as an incident and exits.
    Pitch marks may legally be shorter than the wave frame count, so only
    the waveform needs padding.
    """
    if not s16_path.exists():
        return
    size = s16_path.stat().st_size
    if size % 2:
        with s16_path.open("r+b") as handle:
            handle.truncate(size - 1)
    samples = s16_path.stat().st_size // 2
    remainder = samples % period
    if remainder == 0:
        return
    padding = (period - remainder) * 2
    with s16_path.open("ab") as handle:
        handle.write(b"\x00" * padding)
    print(f"  padded {s16_path.name} with {padding // 2} samples")


def scale_resynth_output(s16_path: Path, gain: float) -> None:
    """Apply a fixed gain to a resynthesized unit without changing its level
    relative to the other units."""
    data = array.array("h")
    data.frombytes(s16_path.read_bytes())
    scaled = array.array(
        "h", (max(-32768, min(32767, int(round(value * gain)))) for value in data)
    )
    s16_path.write_bytes(scaled.tobytes())


def write_pho_files(out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    files = {
        "see": ["_ 60", "s 130", "i: 240 50 120", "_ 60"],
        "tap": ["_ 60", "t 90", "a 170 50 120", "p 130", "_ 60"],
        "pat": ["_ 60", "p 90", "a 170 50 120", "t 130", "_ 60"],
        "all": [
            "_ 60",
            "s 130",
            "i: 240 50 120",
            "_ 80",
            "t 90",
            "a 170 50 120",
            "p 130",
            "_ 80",
            "p 90",
            "a 170 50 120",
            "t 130",
            "_ 60",
        ],
    }
    for name, lines in files.items():
        (out_dir / f"{name}.pho").write_text("\n".join(lines) + "\n", encoding="ascii")


def wav_stats(path: Path) -> Dict[str, float]:
    with wave.open(str(path), "rb") as handle:
        rate = handle.getframerate()
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        frames = handle.getnframes()
        data = array.array("h")
        data.frombytes(handle.readframes(frames))
    if channels > 1:
        data = array.array("h", data[::channels])
    rms = math.sqrt(sum(x * x for x in data) / max(1, len(data)))
    peak = max((abs(x) for x in data), default=0)
    return {
        "rate": rate,
        "channels": channels,
        "width": width,
        "duration_s": frames / rate if rate else 0.0,
        "rms": rms,
        "peak": peak,
    }


def synthesize(db_paths: Dict[int, Path], pho_dir: Path, wav_dir: Path) -> List[Dict[str, object]]:
    wav_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for rate, db_path in db_paths.items():
        print(f"[mbrola] {rate} Hz database")
        for pho in sorted(pho_dir.glob("*.pho")):
            out = wav_dir / f"{db_path.name}_{pho.stem}.wav"
            run([MBROLA, db_path, pho, out], quiet=True)
            stats = wav_stats(out)
            results.append({"database": db_path.name, "pho": pho.stem, "wav": str(out), **stats})
            print(
                f"  {out.name}: {stats['rate']} Hz, {stats['duration_s']:.3f}s, "
                f"rms={stats['rms']:.0f}, peak={stats['peak']}"
            )
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rates", default="16000,22050")
    parser.add_argument("--out", default=str(PROJECT_ROOT / "output" / "mbrola_poc"))
    parser.add_argument("--pad", type=float, default=0.12)
    parser.add_argument("--silence-half", type=float, default=0.10)
    args = parser.parse_args()

    for tool in (ANAF0, ANAMBE, RESYNTH, DATABASE_BUILD, MBROLA):
        if not tool.exists():
            print(f"missing tool: {tool}", file=sys.stderr)
            return 1

    unit_rows = load_csv(UNIT_GRID)
    segments = {int(row["script_line"]): row for row in load_csv(SEGMENTS)}
    specs = build_units(unit_rows, segments, args.silence_half)
    print(f"Units: {', '.join(spec.diphone for spec in specs)}")

    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)
    rates = [int(value) for value in args.rates.split(",") if value.strip()]
    db_paths: Dict[int, Path] = {}
    for rate in rates:
        db_paths[rate] = run_build(rate, specs, out_root, args.pad)

    pho_dir = out_root / "pho"
    write_pho_files(pho_dir)
    results = synthesize(db_paths, pho_dir, out_root / "wav")

    # Keep measurements for databases that were not rebuilt in this run.
    results_path = out_root / "synth_results.json"
    merged: List[Dict[str, object]] = []
    if results_path.exists():
        try:
            previous = json.loads(results_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            previous = []
        if isinstance(previous, list):
            rebuilt = {db_path.name for db_path in db_paths.values()}
            merged = [row for row in previous if row.get("database") not in rebuilt]
    merged.extend(results)
    merged.sort(key=lambda row: (str(row.get("database")), str(row.get("pho"))))
    results_path.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
