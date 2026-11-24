"""On-demand eSpeak pronunciation reference audio rendering and playback.

Renders single prompts in memory (for instant GUI studio playback, especially
auto-playing nonsense supplement lines) or exports per-line WAVs / a track for
external-recorder sessions.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import wave
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

import numpy as np

from .paths import get_espeak_paths
from .project import VoiceProject


def render_prompt_wav_bytes(
    item: Dict[str, object],
    voice: str,
    vowels: Sequence[str],
    *,
    speed: int = 150,
    pitch: int = 70,
    pitch_range: int = 30,
) -> bytes:
    """Render one prompt from `diphone_map.json` to in-memory WAV bytes via bundled eSpeak NG."""
    espeak_bin, espeak_data = get_espeak_paths()
    if not espeak_bin.is_file():
        raise FileNotFoundError(f"Bundled eSpeak NG not found: {espeak_bin}")

    kind = str(item.get("kind", "word"))
    if kind == "word":
        # Real words speak naturally through eSpeak text input
        utterance = str(item["text"])
    else:
        # Supplements / logatomes MUST use eSpeak phoneme input [[...]] with explicit
        # primary stress on the first vowel so eSpeak never reads letters or reduces the vowel.
        vowel_set = set(vowels)
        phones = list(item.get("phones", []))
        stressed: List[str] = []
        marked = False
        for ph in phones:
            if (not marked) and ph in vowel_set:
                stressed.append("'" + ph)
                marked = True
            else:
                stressed.append(ph)
        utterance = "[[" + " ".join(stressed) + "]]"

    env = os.environ.copy()
    env["ESPEAK_DATA_PATH"] = str(espeak_data.resolve())
    cmd = [
        str(espeak_bin),
        "-v", voice,
        "-s", str(speed),
        "-p", str(pitch),
        "-P", str(pitch_range),
        "--stdout",
        utterance,
    ]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    res = subprocess.run(cmd, capture_output=True, env=env, creationflags=creationflags)
    if res.returncode != 0 or not res.stdout:
        raise RuntimeError(f"eSpeak reference render failed for {utterance!r}: {res.stderr.decode('utf-8', 'replace')}")
    return res.stdout


def wav_bytes_to_float32(wav_bytes: bytes) -> Tuple[np.ndarray, int]:
    """Decode mono/stereo PCM WAV bytes into `(float32_array, sample_rate)`."""
    with wave.open(io.BytesIO(wav_bytes), "rb") as wf:
        sr = wf.getframerate()
        ch = wf.getnchannels()
        sw = wf.getsampwidth()
        raw = wf.readframes(wf.getnframes())
    if sw == 2:
        arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif sw == 1:
        arr = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        raise ValueError(f"Unsupported WAV sample width: {sw}")
    if ch > 1:
        arr = arr.reshape(-1, ch)
    return arr, sr


def play_prompt_reference(
    item: Dict[str, object],
    voice: str,
    vowels: Sequence[str],
    *,
    blocking: bool = False,
) -> None:
    """Render and play a single prompt's eSpeak pronunciation reference via sounddevice."""
    import sounddevice as sd

    wav_bytes = render_prompt_wav_bytes(item, voice, vowels)
    audio, sr = wav_bytes_to_float32(wav_bytes)
    sd.stop()
    sd.play(audio, sr, blocking=blocking)


def export_reference_wavs(
    project: VoiceProject,
    out_dir: Path,
    *,
    only_supplements: bool = False,
    lines: Optional[Sequence[int]] = None,
) -> List[Path]:
    """Export per-line eSpeak pronunciation reference WAVs for external recording sessions."""
    inv = json.loads((project.plan_dir / "inventory.json").read_text(encoding="utf-8"))
    dmap = json.loads((project.plan_dir / "diphone_map.json").read_text(encoding="utf-8"))
    vowels = inv["vowels"]
    subset = set(lines) if lines is not None else None

    out_dir.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []
    for item in dmap["lines"]:
        ln = int(item["line"])
        if subset is not None and ln not in subset:
            continue
        if only_supplements and item.get("kind") == "word":
            continue
        wav_bytes = render_prompt_wav_bytes(item, project.config.espeak_voice, vowels)
        dest = out_dir / f"ref_{ln:04d}.wav"
        dest.write_bytes(wav_bytes)
        written.append(dest)
    return written
