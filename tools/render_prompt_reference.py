#!/usr/bin/env python3
"""
Render an audio pronunciation reference for the prompts in a recording script.

The prompt text is the phone string the aligner needs. That is not something a
speaker can be expected to interpret: ``0``, ``S`` against ``s`` and ``T``
against ``t`` carry no clue for a lay reader. This tool renders each prompt
through eSpeak with its phoneme input, so the speaker hears the target instead
of guessing at symbols.

Two ways to use it:

    --play    render each prompt in memory and play it while the speaker works
              through the list: stop on a line, listen, say it, move on. No
              audio files are written.
    (default) write one WAV per prompt, plus an index. The continuous track is
              optional (--track), because most people follow the per-line files
              or the player rather than a 37-minute recording.

The reference is stock eSpeak, not the finished voice: it says which sounds to
make and nothing about timbre.

Usage:
    python tools/render_prompt_reference.py --play
    python tools/render_prompt_reference.py --play --start-line 240 --speed 150
    python tools/render_prompt_reference.py --track --announce
    python tools/render_prompt_reference.py --lines 1-40 --out output/prompt_ref_test
"""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
ESPEAK_BIN = SCRIPT_DIR / "espeak-ng" / "espeak-ng.exe"
ESPEAK_DATA = SCRIPT_DIR / "espeak-ng"

# Characters eSpeak adds to -x output that are not part of a phoneme name:
# stress marks, syllable separators and pauses.
_STRIP = str.maketrans("", "", "',;")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Render an eSpeak pronunciation reference for a prompt list."
    )
    parser.add_argument(
        "--diphone-map",
        type=Path,
        default=PROJECT_ROOT / "output" / "diphone_map.json",
        help="diphone_map.json written by generate_recording_script.py.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=PROJECT_ROOT / "output" / "prompt_ref",
        help="Output directory for file mode. The reference goes in its ref/ "
             "subdirectory. Unused with --play.",
    )
    parser.add_argument(
        "--voice",
        default="en-us",
        help="eSpeak voice to render with, from the language pack (default: en-us).",
    )
    parser.add_argument(
        "--phonemes",
        type=Path,
        default=PROJECT_ROOT / "data" / "phonemes.json",
        help="Inventory used to find the vowel that carries the stress mark.",
    )
    parser.add_argument(
        "--speed", type=int, default=150,
        help="Speaking rate in wpm (default: 150). Much below 140 the eSpeak "
             "formant synthesis starts to sound unsteady on sustained vowels.",
    )
    parser.add_argument(
        "--pitch", type=int, default=70, help="Base pitch 0-99 (default: 70)."
    )
    parser.add_argument(
        "--range", type=int, default=0,
        help="Pitch range 0-99. 0 flattens the melody so the phones carry the "
             "information (default: 0).",
    )
    parser.add_argument(
        "--gap-ms", type=int, default=800,
        help="Silence between prompts in the track (default: 800).",
    )
    parser.add_argument(
        "--announce",
        action="store_true",
        help="Speak the line number before each prompt. Useful when following "
             "the track by ear, noisy when watching the screen.",
    )
    parser.add_argument(
        "--track",
        action="store_true",
        help="Also write track.wav, the whole set in one file. Off by default; "
             "the per-line player is the better way to work.",
    )
    parser.add_argument(
        "--play",
        action="store_true",
        help="Interactive mode: render in memory and play each prompt, then wait "
             "for a key. Writes no audio files.",
    )
    parser.add_argument(
        "--start-line",
        type=int,
        default=None,
        help="First prompt line to use, for resuming a session.",
    )
    parser.add_argument(
        "--lines",
        default=None,
        help="Render only this line range, for example 1-40.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Parallel eSpeak processes in file mode (default: 4).",
    )
    return parser.parse_args()


def espeak_env() -> dict:
    env = os.environ.copy()
    env["ESPEAK_DATA_PATH"] = str(ESPEAK_DATA)
    return env


def load_vowels(path: Path) -> set[str]:
    """Vowel names from the inventory, used to place the stress mark."""
    if not path.exists():
        return set()
    with path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    return set(data.get("vowels", [])) | set(data.get("vowels_core", []))


def parse_lines(spec: str | None, start: int | None, lines: list[dict]) -> list[dict]:
    selected = lines
    if spec:
        try:
            first, last = spec.split("-", 1)
            lo, hi = int(first), int(last)
        except ValueError:
            raise SystemExit(f"--lines must look like 1-40, not {spec!r}")
        selected = [item for item in selected if lo <= item["line"] <= hi]
    if start is not None:
        selected = [item for item in selected if item["line"] >= start]
    if not selected:
        raise SystemExit("no prompts selected")
    return selected


def build_target(phones: list[str], vowels: set[str]) -> str:
    """Phoneme-input string for a prompt, with the nucleus stressed.

    eSpeak reduces an unstressed phoneme-input vowel: ``[[p I]]`` renders the
    happy vowel, not KIT, and only ``[[p 'I]]`` gives the intended sound. The
    stress mark is what keeps the prompt's own vowel.
    """
    tokens = list(phones)
    for index, token in enumerate(tokens):
        if token in vowels:
            tokens[index] = "'" + token
            break
    return "[[%s]]" % " ".join(tokens)


def render_wav(target: str, args: argparse.Namespace) -> bytes:
    """Render a target to WAV bytes, in memory."""
    return subprocess.run(
        [
            str(ESPEAK_BIN),
            "-v", args.voice,
            "-s", str(args.speed),
            "-p", str(args.pitch),
            "-P", str(args.range),
            "-z",
            "--stdout",
            target,
        ],
        env=espeak_env(),
        check=True,
        capture_output=True,
    ).stdout


def render_text(text: str, args: argparse.Namespace) -> bytes:
    return subprocess.run(
        [
            str(ESPEAK_BIN),
            "-v", args.voice,
            "-s", str(args.speed),
            "-p", str(args.pitch),
            "-P", str(args.range),
            "-z",
            "--stdout",
            text,
        ],
        env=espeak_env(),
        check=True,
        capture_output=True,
    ).stdout


def check_echo(target: str, phones: list[str], args: argparse.Namespace) -> None:
    """Warn when eSpeak renders something other than the requested phones."""
    echo = subprocess.run(
        [str(ESPEAK_BIN), "-v", args.voice, "-q", "-x", target],
        env=espeak_env(),
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    produced = [tok for tok in echo.translate(_STRIP).split() if tok not in ("-", "_")]
    if produced != phones:
        print(
            f"warning: asked for {' '.join(phones)} but eSpeak produced "
            f"{' '.join(produced)}",
            file=sys.stderr,
        )


def wav_params(data: bytes) -> tuple[int, int, int]:
    with wave.open(io.BytesIO(data), "rb") as handle:
        return handle.getnchannels(), handle.getsampwidth(), handle.getframerate()


def wav_frames(data: bytes) -> bytes:
    with wave.open(io.BytesIO(data), "rb") as handle:
        return handle.readframes(handle.getnframes())


def read_key() -> str:
    import msvcrt

    char = msvcrt.getwch()
    if char in ("\x00", "\xe0"):  # arrow and function keys
        msvcrt.getwch()
        return ""
    return char


def play_loop(items: list[dict], args: argparse.Namespace, vowels: set[str]) -> None:
    try:
        import winsound
    except ImportError:
        raise SystemExit("--play needs Windows and the winsound module")

    print("enter or space: next   r: repeat   p: previous   q: quit")
    index = 0
    while 0 <= index < len(items):
        item = items[index]
        target = build_target(item["phones"], vowels)
        print(f"\n[{index + 1}/{len(items)}] line {item['line']:04d}: "
              f"{' '.join(item['phones'])}", flush=True)
        check_echo(target, item["phones"], args)
        winsound.PlaySound(render_wav(target, args), winsound.SND_MEMORY)
        key = read_key().lower()
        if key in ("\r", " ", ""):
            index += 1
        elif key == "r":
            continue
        elif key == "p":
            index = max(0, index - 1)
        elif key == "q" or key == "\x1b":
            break


def render_file_entry(
    item: dict, args: argparse.Namespace, vowels: set[str], ref_dir: Path
) -> dict:
    target = build_target(item["phones"], vowels)
    data = render_wav(target, args)
    wav_path = ref_dir / f"{item['line']:04d}.wav"
    wav_path.write_bytes(data)
    return {
        "line": item["line"],
        "unit_type": item.get("unit_type"),
        "phones": item["phones"],
        "file": wav_path.name,
        "target": target,
        "params": wav_params(data),
        "frames": wav_frames(data),
    }


def write_files(items: list[dict], args: argparse.Namespace, vowels: set[str]) -> None:
    ref_dir = args.out / "ref"
    ref_dir.mkdir(parents=True, exist_ok=True)

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        entries = list(
            pool.map(lambda item: render_file_entry(item, args, vowels, ref_dir), items)
        )

    for entry in entries:
        check_echo(entry["target"], entry["phones"], args)

    nchannels, sampwidth, framerate = entries[0]["params"]
    for entry in entries:
        if entry["params"] != (nchannels, sampwidth, framerate):
            raise SystemExit(f"render format changed at prompt {entry['line']}")

    track_path = args.out / "ref" / "track.wav"
    gap = b"\x00" * int(framerate * args.gap_ms / 1000) * nchannels * sampwidth
    items_out: list[dict] = []
    position_frames = 0

    track = wave.open(str(track_path), "wb") if args.track else None
    if track is not None:
        track.setnchannels(nchannels)
        track.setsampwidth(sampwidth)
        track.setframerate(framerate)

    try:
        for entry in entries:
            if track is not None and args.announce:
                number = render_text(str(entry["line"]), args)
                track.writeframes(wav_frames(number))
                position_frames += len(wav_frames(number)) // (nchannels * sampwidth)

            if track is not None:
                track.writeframes(entry["frames"])
                start_s = position_frames / framerate
                position_frames += len(entry["frames"]) // (nchannels * sampwidth)
            else:
                start_s = None
            duration_s = len(entry["frames"]) / (nchannels * sampwidth) / framerate

            items_out.append({
                "line": entry["line"],
                "unit_type": entry["unit_type"],
                "phones": entry["phones"],
                "file": entry["file"],
                "start_s": None if start_s is None else round(start_s, 4),
                "duration_s": round(duration_s, 4),
            })

            if track is not None:
                track.writeframes(gap)
                position_frames += len(gap) // (nchannels * sampwidth)
    finally:
        if track is not None:
            track.close()

    index = {
        "voice": args.voice,
        "speed": args.speed,
        "pitch": args.pitch,
        "pitch_range": args.range,
        "gap_ms": args.gap_ms,
        "announce_numbers": bool(args.announce),
        "sample_rate": framerate,
        "channels": nchannels,
        "sample_width": sampwidth,
        "track": track_path.name if args.track else None,
        "items": items_out,
    }
    (args.out / "ref" / "index.json").write_text(
        json.dumps(index, indent=2) + "\n", encoding="utf-8"
    )

    print(f"Rendered {len(items_out)} prompts with {args.voice} into {ref_dir}")
    if args.track:
        print(f"Track: {track_path} ({position_frames / framerate / 60:.1f} minutes)")
    print("Index: " + str(args.out / "ref" / "index.json"))


def main() -> None:
    args = parse_args()

    if not ESPEAK_BIN.exists():
        raise SystemExit(f"eSpeak NG not found: {ESPEAK_BIN}")
    if not args.diphone_map.exists():
        raise SystemExit(f"prompt list not found: {args.diphone_map}")

    data = json.loads(args.diphone_map.read_text(encoding="utf-8"))
    items = parse_lines(args.lines, args.start_line, data["lines"])
    vowels = load_vowels(args.phonemes)

    if args.play:
        play_loop(items, args, vowels)
    else:
        write_files(items, args, vowels)


if __name__ == "__main__":
    main()
