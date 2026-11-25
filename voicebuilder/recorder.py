"""Audio capture, room-tone calibration, instant take quality check, and playback via sounddevice."""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
import sounddevice as sd
import soundfile as sf


RECORD_SAMPLE_RATE = 44100
RECORD_CHANNELS = 1


@dataclass(frozen=True)
class InputDevice:
    index: int
    name: str
    max_input_channels: int
    default_samplerate: float

    @property
    def label(self) -> str:
        return f"{self.name} ({int(round(self.default_samplerate))} Hz)"


@dataclass(frozen=True)
class TakeQuality:
    duration_s: float
    peak: float
    rms_db: float
    noise_db: float
    snr_db: float
    clipped: bool
    too_quiet: bool
    too_short: bool
    low_snr: bool

    @property
    def ok(self) -> bool:
        return not (self.clipped or self.too_quiet or self.too_short or self.low_snr)

    @property
    def verdict(self) -> str:
        issues: List[str] = []
        if self.clipped:
            issues.append(f"CLIPPED (peak {self.peak:.2f}) — move slightly back or lower mic gain")
        if self.too_short:
            issues.append(f"TOO SHORT ({self.duration_s:.2f} s)")
        if self.too_quiet:
            issues.append(f"TOO QUIET ({self.rms_db:.1f} dBFS)")
        if self.low_snr:
            issues.append(f"LOW SNR ({self.snr_db:.1f} dB above noise)")
        if not issues:
            return f"GOOD — {self.duration_s:.2f} s, peak {self.peak:.2f}, SNR {self.snr_db:.1f} dB"
        return "CHECK TAKE: " + "; ".join(issues)


def list_input_devices() -> List[InputDevice]:
    """Return available audio input devices (microphones)."""
    devices: List[InputDevice] = []
    try:
        raw = sd.query_devices()
    except Exception:
        return devices
    for idx, info in enumerate(raw):
        if int(info.get("max_input_channels", 0)) > 0:
            devices.append(
                InputDevice(
                    index=idx,
                    name=str(info.get("name", f"Device {idx}")),
                    max_input_channels=int(info.get("max_input_channels", 1)),
                    default_samplerate=float(info.get("default_samplerate", RECORD_SAMPLE_RATE)),
                )
            )
    return devices


def default_input_device_index() -> Optional[int]:
    try:
        default_in = sd.default.device[0]
        if default_in is not None and int(default_in) >= 0:
            return int(default_in)
    except Exception:
        pass
    devs = list_input_devices()
    return devs[0].index if devs else None


def _rms_db(arr: np.ndarray) -> float:
    if len(arr) == 0:
        return -120.0
    rms = float(np.sqrt(np.mean(arr.astype(np.float64) ** 2)))
    return 20.0 * math.log10(max(rms, 1e-12))


def analyze_take(
    audio: np.ndarray,
    sr: int,
    *,
    calibrated_noise_db: Optional[float] = None,
) -> TakeQuality:
    """Run instant clipping, level, SNR, and duration diagnostics on a recorded take."""
    mono = audio[:, 0] if audio.ndim > 1 else audio
    mono = np.asarray(mono, dtype=np.float32)
    dur = len(mono) / float(sr) if sr > 0 else 0.0
    peak = float(np.max(np.abs(mono))) if len(mono) else 0.0

    hop = max(1, int(sr * 0.010))
    win = max(hop, int(sr * 0.020))
    if len(mono) >= win:
        n_frames = (len(mono) - win) // hop + 1
        cs = np.concatenate(([0.0], np.cumsum(mono.astype(np.float64) ** 2)))
        idx = np.arange(n_frames) * hop
        frame_rms = np.sqrt(np.maximum(0.0, (cs[idx + win] - cs[idx]) / win))
        frame_db = 20.0 * np.log10(np.maximum(frame_rms, 1e-12))
        local_noise = float(np.percentile(frame_db, 15))
        speech_rms = float(np.percentile(frame_db, 90))
    else:
        local_noise = _rms_db(mono)
        speech_rms = local_noise

    noise_db = min(local_noise, calibrated_noise_db) if calibrated_noise_db is not None else local_noise
    snr_db = speech_rms - noise_db

    return TakeQuality(
        duration_s=round(dur, 3),
        peak=round(peak, 4),
        rms_db=round(speech_rms, 1),
        noise_db=round(noise_db, 1),
        snr_db=round(snr_db, 1),
        clipped=peak >= 0.99,
        too_quiet=speech_rms < -38.0,
        too_short=dur < 0.20,
        low_snr=snr_db < 14.0,
    )


def calibrate_room_noise(device: Optional[int] = None, duration_s: float = 2.5) -> Tuple[float, float]:
    """Record `duration_s` of room silence and return `(noise_rms_db, peak)`."""
    frames = int(round(duration_s * RECORD_SAMPLE_RATE))
    rec = sd.rec(frames, samplerate=RECORD_SAMPLE_RATE, channels=1, dtype="float32", device=device)
    sd.wait()
    mono = rec[:, 0] if rec.ndim > 1 else rec
    return round(_rms_db(mono), 1), round(float(np.max(np.abs(mono))), 4)


def play_wav_file(path: Path, *, blocking: bool = False) -> None:
    """Play a WAV file through the default output device."""
    audio, sr = sf.read(str(path), dtype="float32", always_2d=False)
    sd.stop()
    sd.play(audio, sr, blocking=blocking)


def stop_playback() -> None:
    try:
        sd.stop()
    except Exception:
        pass


def play_warning_tone(*, blocking: bool = True) -> None:
    """Play a brief, non-speech two-tone descending cue (~200 ms) signaling a failed take."""
    sr = RECORD_SAMPLE_RATE

    def _tone(freq_hz: float, dur_s: float, amp: float = 0.28) -> np.ndarray:
        n = max(1, int(round(dur_s * sr)))
        t = np.arange(n, dtype=np.float32) / float(sr)
        wave = np.sin(2.0 * math.pi * freq_hz * t) * amp
        fade = min(int(0.008 * sr), n // 4)
        if fade > 1:
            env = np.linspace(0.0, 1.0, fade, dtype=np.float32)
            wave[:fade] *= env
            wave[-fade:] *= env[::-1]
        return wave

    gap = np.zeros(int(0.025 * sr), dtype=np.float32)
    cue = np.concatenate([_tone(360.0, 0.075), gap, _tone(240.0, 0.105)])
    try:
        sd.stop()
        sd.play(cue, sr, blocking=blocking)
    except Exception:
        pass


def save_take_wav(audio: np.ndarray, dest_wav: Path, sample_rate: int = RECORD_SAMPLE_RATE) -> None:
    """Atomically write a mono 16-bit PCM WAV take."""
    mono = audio[:, 0] if audio.ndim > 1 else audio
    mono = np.asarray(mono, dtype=np.float32)
    dest_wav.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest_wav.with_suffix(".wav.tmp")
    sf.write(str(tmp), mono, sample_rate, subtype="PCM_16", format="WAV")
    tmp.replace(dest_wav)


class TakeRecorder:
    """Non-blocking toggle recorder (`start()` -> `stop_and_save(dest_wav)`)."""

    def __init__(self, sample_rate: int = RECORD_SAMPLE_RATE) -> None:
        self.sample_rate = sample_rate
        self._lock = threading.Lock()
        self._chunks: List[np.ndarray] = []
        self._stream: Optional[sd.InputStream] = None
        self._recording = False

    @property
    def is_recording(self) -> bool:
        with self._lock:
            return self._recording

    def start(self, device: Optional[int] = None, *, stop_audio: bool = True) -> None:
        if stop_audio:
            stop_playback()
        with self._lock:
            if self._recording:
                return
            self._chunks = []
            self._recording = True

        def _callback(indata: np.ndarray, frames: int, time_info: object, status: object) -> None:
            with self._lock:
                if self._recording:
                    self._chunks.append(indata.copy())

        stream = sd.InputStream(
            samplerate=self.sample_rate,
            channels=RECORD_CHANNELS,
            dtype="float32",
            device=device,
            callback=_callback,
        )
        stream.start()
        with self._lock:
            self._stream = stream

    def clear_buffer(self) -> None:
        """Discard buffered audio for the current line while keeping the input stream open."""
        with self._lock:
            self._chunks = []

    def _chunks_to_mono(self, chunks: List[np.ndarray]) -> np.ndarray:
        if chunks:
            audio = np.concatenate(chunks, axis=0)
            if audio.ndim > 1:
                audio = audio[:, 0]
            return np.asarray(audio, dtype=np.float32)
        return np.zeros(0, dtype=np.float32)

    def _write_chunks(
        self,
        chunks: List[np.ndarray],
        dest_wav: Path,
        *,
        calibrated_noise_db: Optional[float] = None,
    ) -> TakeQuality:
        audio = self._chunks_to_mono(chunks)
        save_take_wav(audio, dest_wav, self.sample_rate)
        return analyze_take(audio, self.sample_rate, calibrated_noise_db=calibrated_noise_db)

    def flush_buffer(self) -> np.ndarray:
        """Atomically extract and clear the buffered audio while keeping the mic stream open."""
        with self._lock:
            chunks = list(self._chunks)
            self._chunks = []
        return self._chunks_to_mono(chunks)

    def stop_buffer(self) -> np.ndarray:
        """Stop and close the mic stream, returning the buffered mono float32 audio."""
        with self._lock:
            self._recording = False
            stream = self._stream
            self._stream = None
            chunks = list(self._chunks)
            self._chunks = []
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        return self._chunks_to_mono(chunks)

    def flush_and_save_keep_recording(
        self,
        dest_wav: Path,
        *,
        calibrated_noise_db: Optional[float] = None,
    ) -> TakeQuality:
        """Save the current take atomically and immediately roll into a fresh buffer without closing the mic."""
        with self._lock:
            chunks = list(self._chunks)
            self._chunks = []
        return self._write_chunks(chunks, dest_wav, calibrated_noise_db=calibrated_noise_db)

    def abort(self) -> None:
        """Force-stop an in-progress recording without saving (e.g. at transition gates)."""
        with self._lock:
            self._recording = False
            stream = self._stream
            self._stream = None
            self._chunks = []
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass

    def stop_and_save(
        self,
        dest_wav: Path,
        *,
        calibrated_noise_db: Optional[float] = None,
    ) -> TakeQuality:
        """Stop capture, atomically write 44.1 kHz 16-bit mono WAV, and return quality diagnostics."""
        with self._lock:
            self._recording = False
            stream = self._stream
            self._stream = None
            chunks = list(self._chunks)
            self._chunks = []
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        return self._write_chunks(chunks, dest_wav, calibrated_noise_db=calibrated_noise_db)
