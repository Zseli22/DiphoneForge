"""Clean-slate on-demand downloader & environment doctor.

Guarantees that a first-time user with zero pre-installed tools can fetch:
  1. `micromamba.exe` -> `<cache>/bin/micromamba.exe`
  2. MFA conda environment -> `<cache>/mfa-env/` (auto-detects existing install first)
  3. FrequencyWords word lists -> `<cache>/wordlists/<lang>_50k.txt`
  4. MFA acoustic models -> `<cache>/mfa_root/pretrained_models/acoustic/<model>.zip`
  5. Cached `mfa model inspect` phone inventories -> `<cache>/mfa_root/inspect_cache/<model>.json`

All downloads use atomic `.tmp` -> rename so interrupted transfers never leave
corrupt cache files behind.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import tarfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .catalog import LanguageCatalog, LanguageSpec
from .paths import (
    find_mfa_env,
    find_micromamba,
    find_preview_engine_dir,
    get_bin_cache_dir,
    get_cache_root,
    get_default_mfa_env_dir,
    get_espeak_paths,
    get_mbrolator_binaries,
    get_mfa_inspect_cache_dir,
    get_mfa_root_dir,
    get_wordlists_cache_dir,
    mfa_subprocess_env,
)

ProgressFn = Callable[[str, Optional[float]], None]
CancelFn = Callable[[], bool]

MICROMAMBA_WIN64_URL = "https://micro.mamba.pm/api/micromamba/win-64/latest"
USER_AGENT = "DiphoneForge-VoiceBuilder/0.1"


class CancelledError(RuntimeError):
    """Raised when the user cancels a long-running download or pipeline step."""


@dataclass
class ComponentStatus:
    name: str
    ok: bool
    detail: str
    path: Optional[str] = None
    downloadable: bool = False


def _report(cb: Optional[ProgressFn], msg: str, frac: Optional[float] = None) -> None:
    if cb is not None:
        cb(msg, frac)


def _check_cancel(cancel: Optional[CancelFn]) -> None:
    if cancel is not None and cancel():
        raise CancelledError("Operation cancelled by user.")


def download_file_atomic(
    url: str,
    dest: Path,
    *,
    progress: Optional[ProgressFn] = None,
    cancel: Optional[CancelFn] = None,
    label: Optional[str] = None,
    timeout: int = 60,
) -> Path:
    """Stream `url` to `dest` atomically via `.tmp` with progress and cancellation."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tag = label or dest.name
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            total_hdr = resp.headers.get("Content-Length")
            total = int(total_hdr) if total_hdr and total_hdr.isdigit() else 0
            received = 0
            with tmp.open("wb") as out:
                while True:
                    _check_cancel(cancel)
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    out.write(chunk)
                    received += len(chunk)
                    if total > 0:
                        frac = min(1.0, received / float(total))
                        _report(
                            progress,
                            f"Downloading {tag}: {received / 1e6:.2f} / {total / 1e6:.2f} MB",
                            frac,
                        )
                    else:
                        _report(progress, f"Downloading {tag}: {received / 1e6:.2f} MB", None)
        if tmp.stat().st_size == 0:
            raise RuntimeError(f"Downloaded empty file from {url}")
        tmp.replace(dest)
        return dest
    except BaseException:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise


def ensure_wordlist(
    lang: LanguageSpec,
    *,
    catalog: Optional[LanguageCatalog] = None,
    progress: Optional[ProgressFn] = None,
    cancel: Optional[CancelFn] = None,
    force: bool = False,
) -> Path:
    """Return cached FrequencyWords file for `lang`, downloading on demand if absent."""
    if not lang.wordlist_code:
        raise ValueError(
            f"Language '{lang.name}' has no FrequencyWords list; supply a custom word list or plain text file."
        )
    cat = catalog or LanguageCatalog()
    wl_dir = get_wordlists_cache_dir()
    wl_dir.mkdir(parents=True, exist_ok=True)
    dest_50k = wl_dir / f"{lang.wordlist_code}_50k.txt"
    dest_full = wl_dir / f"{lang.wordlist_code}_full.txt"

    if not force:
        for existing in (dest_50k, dest_full):
            if existing.is_file() and existing.stat().st_size > 1024:
                _report(progress, f"Using cached word list: {existing.name}", 1.0)
                return existing

    urls = cat.wordlist_urls(lang.wordlist_code)
    dests = [dest_50k, dest_full]
    last_err: Optional[Exception] = None
    for url, dest in zip(urls, dests):
        try:
            _report(progress, f"Fetching word list for {lang.name}...", 0.0)
            return download_file_atomic(url, dest, progress=progress, cancel=cancel, label=dest.name)
        except urllib.error.HTTPError as exc:
            last_err = exc
            if exc.code == 404:
                continue
            raise
    raise RuntimeError(f"Could not download word list for {lang.id} ({lang.wordlist_code}): {last_err}")


def ensure_micromamba(
    *,
    progress: Optional[ProgressFn] = None,
    cancel: Optional[CancelFn] = None,
) -> Path:
    """Locate or download standalone Windows `micromamba.exe` into `<cache>/bin/`."""
    existing = find_micromamba()
    if existing is not None:
        return existing

    bin_dir = get_bin_cache_dir()
    bin_dir.mkdir(parents=True, exist_ok=True)
    archive = bin_dir / "micromamba-win64.tar.bz2"
    target = bin_dir / "micromamba.exe"
    tmp_exe = bin_dir / "micromamba.exe.tmp"

    _report(progress, "Downloading micromamba.exe (~10 MB)...", 0.0)
    download_file_atomic(
        MICROMAMBA_WIN64_URL,
        archive,
        progress=progress,
        cancel=cancel,
        label="micromamba",
        timeout=120,
    )
    try:
        _report(progress, "Extracting micromamba.exe...", 0.9)
        extracted = False
        with tarfile.open(archive, "r:bz2") as tf:
            for member in tf.getmembers():
                if member.name.lower().endswith("micromamba.exe"):
                    f = tf.extractfile(member)
                    if f is not None:
                        with tmp_exe.open("wb") as out:
                            shutil.copyfileobj(f, out)
                        tmp_exe.replace(target)
                        extracted = True
                        break
        if not extracted or not target.is_file():
            raise RuntimeError("Downloaded micromamba archive did not contain micromamba.exe")
        _report(progress, f"Installed micromamba at {target}", 1.0)
        return target
    finally:
        if archive.exists():
            try:
                archive.unlink()
            except OSError:
                pass
        if tmp_exe.exists():
            try:
                tmp_exe.unlink()
            except OSError:
                pass


def ensure_mfa_env(
    *,
    progress: Optional[ProgressFn] = None,
    cancel: Optional[CancelFn] = None,
    ignore_external: bool = False,
) -> Path:
    """Locate an existing MFA runtime or bootstrap a fresh one in `<cache>/mfa-env/`."""
    existing = find_mfa_env(ignore_external=ignore_external)
    if existing is not None:
        _report(progress, f"Found MFA environment at {existing}", 1.0)
        return existing

    mm = ensure_micromamba(progress=progress, cancel=cancel)
    target = get_default_mfa_env_dir()
    target.parent.mkdir(parents=True, exist_ok=True)
    _report(progress, f"Creating MFA environment in {target} (one-time ~2.4 GB download)...", None)

    cmd = [
        str(mm),
        "create",
        "-y",
        "-p",
        str(target),
        "-c",
        "conda-forge",
        "montreal-forced-aligner>=3.1",
        "soundfile",
        "scipy",
    ]
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
            _check_cancel(cancel)
            s = line.strip()
            if s:
                tail.append(s)
                if len(tail) > 40:
                    tail.pop(0)
                _report(progress, f"MFA setup: {s[:110]}", None)
        rc = proc.wait()
    except BaseException:
        proc.terminate()
        raise
    if rc != 0 or not (target / "python.exe").is_file():
        raise RuntimeError(f"micromamba MFA install failed (exit {rc}):\n" + "\n".join(tail[-15:]))
    _report(progress, f"MFA environment ready at {target}", 1.0)
    return target


def get_cached_mfa_model_path(model_name: str) -> Optional[Path]:
    """Return path to cached MFA acoustic model zip inside portable MFA_ROOT_DIR if present."""
    ac_dir = get_mfa_root_dir() / "pretrained_models" / "acoustic"
    direct = ac_dir / f"{model_name}.zip"
    if direct.is_file() and direct.stat().st_size > 10000:
        return direct
    # If the user already has this model in MFA's legacy Documents\MFA cache, reuse it
    # by copying it into our portable cache instead of re-downloading.
    legacy = Path.home() / "Documents" / "MFA" / "pretrained_models" / "acoustic" / f"{model_name}.zip"
    if legacy.is_file() and legacy.stat().st_size > 10000:
        try:
            ac_dir.mkdir(parents=True, exist_ok=True)
            tmp = direct.with_suffix(".zip.tmp")
            shutil.copy2(legacy, tmp)
            tmp.replace(direct)
            return direct
        except OSError:
            pass
    return None


def ensure_mfa_model(
    model_name: str,
    *,
    mfa_env_dir: Optional[Path] = None,
    progress: Optional[ProgressFn] = None,
    cancel: Optional[CancelFn] = None,
) -> Path:
    """Download an MFA acoustic model into `<cache>/mfa_root/pretrained_models/acoustic/` on demand."""
    cached = get_cached_mfa_model_path(model_name)
    if cached is not None:
        _report(progress, f"Using cached MFA model: {model_name}", 1.0)
        return cached

    env_dir = mfa_env_dir or ensure_mfa_env(progress=progress, cancel=cancel)
    _report(progress, f"Downloading MFA acoustic model '{model_name}' (~15-60 MB)...", None)
    cmd = [
        str(env_dir / "python.exe"),
        "-m",
        "montreal_forced_aligner.command_line.mfa",
        "model",
        "download",
        "acoustic",
        model_name,
    ]
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=mfa_subprocess_env(env_dir),
        creationflags=creationflags,
    )
    tail: List[str] = []
    assert proc.stdout is not None
    try:
        for line in proc.stdout:
            _check_cancel(cancel)
            s = line.strip()
            if s:
                tail.append(s)
                if len(tail) > 30:
                    tail.pop(0)
                _report(progress, f"MFA model: {s[:110]}", None)
        rc = proc.wait()
    except BaseException:
        proc.terminate()
        raise
    cached = get_cached_mfa_model_path(model_name)
    if rc != 0 or cached is None:
        raise RuntimeError(f"Failed to download MFA acoustic model '{model_name}' (exit {rc}):\n" + "\n".join(tail[-15:]))
    _report(progress, f"Downloaded MFA model: {cached.name}", 1.0)
    return cached


def inspect_mfa_model_phones(
    model_name: str,
    *,
    mfa_env_dir: Optional[Path] = None,
    progress: Optional[ProgressFn] = None,
    cancel: Optional[CancelFn] = None,
) -> List[str]:
    """Return the exact phone inventory of a cached MFA acoustic model (cached as JSON)."""
    cache_dir = get_mfa_inspect_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"{model_name}.json"

    model_zip = ensure_mfa_model(model_name, mfa_env_dir=mfa_env_dir, progress=progress, cancel=cancel)
    if cache_file.is_file() and cache_file.stat().st_mtime >= model_zip.stat().st_mtime:
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        if data.get("phones"):
            return list(data["phones"])

    # Fast path: MFA acoustic model zips store meta.json directly inside the archive!
    import zipfile

    phones: List[str] = []
    try:
        with zipfile.ZipFile(model_zip, "r") as zf:
            for name in zf.namelist():
                if name.endswith("meta.json"):
                    meta = json.loads(zf.read(name).decode("utf-8"))
                    raw_phones = meta.get("phones") or []
                    if isinstance(raw_phones, list) and raw_phones:
                        phones = [str(p) for p in raw_phones]
                        break
    except Exception:
        phones = []

    if not phones:
        env_dir = mfa_env_dir or ensure_mfa_env(progress=progress, cancel=cancel)
        _report(progress, f"Inspecting phone inventory of {model_name}...", None)
        cmd = [
            str(env_dir / "python.exe"),
            "-m",
            "montreal_forced_aligner.command_line.mfa",
            "model",
            "inspect",
            "acoustic",
            model_name,
        ]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=mfa_subprocess_env(env_dir),
            creationflags=creationflags,
            check=True,
        )
        # Parse phone list from YAML/text inspect output
        m = re.search(r"phones:\s*\[([^\]]+)\]", res.stdout, re.S)
        if m:
            phones = [t.strip().strip("'\"") for t in m.group(1).split(",") if t.strip()]
        else:
            in_phones = False
            for ln in res.stdout.splitlines():
                if re.match(r"^\s*phones\s*:", ln):
                    in_phones = True
                    continue
                if in_phones:
                    mm = re.match(r"^\s*-\s+(.+?)\s*$", ln)
                    if mm:
                        phones.append(mm.group(1).strip().strip("'\""))
                    elif ln.strip() and not ln.startswith(" "):
                        break

    if not phones:
        raise RuntimeError(f"Could not extract phone inventory from MFA model '{model_name}'")

    tmp = cache_file.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"model": model_name, "phones": phones}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(cache_file)
    return phones


def run_doctor(*, ignore_external_mfa: bool = False) -> List[ComponentStatus]:
    """Check status of all bundled and on-demand components for the Setup Doctor UI / CLI."""
    statuses: List[ComponentStatus] = []

    espeak_bin, espeak_data = get_espeak_paths()
    espeak_ok = espeak_bin.is_file() and (espeak_data / "espeak-ng-data").is_dir()
    statuses.append(
        ComponentStatus(
            name="eSpeak NG",
            ok=espeak_ok,
            detail="Ready (bundled)" if espeak_ok else "Missing tools/espeak-ng/espeak-ng.exe",
            path=str(espeak_bin),
            downloadable=False,
        )
    )

    bins = get_mbrolator_binaries()
    missing_bins = [k for k, p in bins.items() if not p.is_file()]
    statuses.append(
        ComponentStatus(
            name="MBROLATOR & MBROLA toolchain",
            ok=not missing_bins,
            detail="Ready (all 5 binaries)" if not missing_bins else f"Missing: {', '.join(missing_bins)}",
            path=str(bins["mbrola"].parent),
            downloadable=False,
        )
    )

    engine = find_preview_engine_dir()
    statuses.append(
        ComponentStatus(
            name="Live Speech Preview Host (32-bit)",
            ok=engine is not None,
            detail="Ready" if engine else "Optional: build add-on or install espeakWitMbrola for live playback",
            path=str(engine) if engine else None,
            downloadable=False,
        )
    )

    mm = find_micromamba()
    statuses.append(
        ComponentStatus(
            name="micromamba bootstrapper",
            ok=mm is not None,
            detail="Ready" if mm else "Will auto-download (~10 MB) when installing MFA",
            path=str(mm) if mm else str(get_bin_cache_dir() / "micromamba.exe"),
            downloadable=True,
        )
    )

    mfa = find_mfa_env(ignore_external=ignore_external_mfa)
    statuses.append(
        ComponentStatus(
            name="Montreal Forced Aligner (MFA)",
            ok=mfa is not None,
            detail=f"Ready ({mfa})" if mfa else "Not installed yet (one-time ~2.4 GB on-demand setup)",
            path=str(mfa) if mfa else str(get_default_mfa_env_dir()),
            downloadable=True,
        )
    )

    wl_dir = get_wordlists_cache_dir()
    wl_count = len(list(wl_dir.glob("*.txt"))) if wl_dir.is_dir() else 0
    ac_dir = get_mfa_root_dir() / "pretrained_models" / "acoustic"
    model_count = len(list(ac_dir.glob("*.zip"))) if ac_dir.is_dir() else 0
    statuses.append(
        ComponentStatus(
            name="Portable Cache",
            ok=True,
            detail=f"{wl_count} word list(s), {model_count} MFA model(s) cached",
            path=str(get_cache_root()),
            downloadable=False,
        )
    )
    return statuses
