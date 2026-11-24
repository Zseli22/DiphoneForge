"""Centralized, portable path resolution for bundled tools and on-demand caches.

Every downloaded asset (micromamba, MFA environment, MFA acoustic models,
FrequencyWords word lists) lands inside `<app_dir>/cache/` by default so a
clean-slate user needs zero manual setup and MFA never scatters files into
`Documents\\MFA`.

Set `DIPHONEFORGE_CACHE_DIR` to override the cache root (handy for clean-slate
testing on a developer machine).
"""

from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional


def _space_free_path(path: Path) -> Path:
    """Resolve junctions/symlinks and, on Windows, fall back to 8.3 short path if spaces remain.

    Upstream Montreal Forced Aligner's `thirdparty_binary()` wraps paths containing spaces in
    literal double quotes and passes them as `argv[0]` to `subprocess.run()`, which triggers
    `PermissionError: [WinError 5] Access is denied` on Windows. Resolving junctions (e.g.
    `tools/mfa-env` -> `C:\\diphone-mfa-env`) and using `GetShortPathNameW` eliminates spaces.
    """
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path
    s = str(resolved)
    if os.name == "nt" and " " in s and resolved.exists():
        try:
            buf = ctypes.create_unicode_buffer(1024)
            n = ctypes.windll.kernel32.GetShortPathNameW(s, buf, len(buf))
            if 0 < n < len(buf) and " " not in buf.value:
                return Path(buf.value)
        except Exception:
            pass
    return resolved


def get_app_root() -> Path:
    """Return the DiphoneForge application / repository root directory."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def get_cache_root() -> Path:
    """Portable on-demand cache directory next to the app (or DIPHONEFORGE_CACHE_DIR)."""
    override = os.environ.get("DIPHONEFORGE_CACHE_DIR", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return get_app_root() / "cache"


def get_wordlists_cache_dir() -> Path:
    return get_cache_root() / "wordlists"


def get_bin_cache_dir() -> Path:
    return get_cache_root() / "bin"


def get_mfa_root_dir() -> Path:
    """Directory passed as MFA_ROOT_DIR for all MFA downloads and temp workspaces."""
    return get_cache_root() / "mfa_root"


def get_mfa_inspect_cache_dir() -> Path:
    return get_mfa_root_dir() / "inspect_cache"


def get_default_mfa_env_dir() -> Path:
    """Target directory when bootstrapping a fresh MFA environment via micromamba."""
    return get_cache_root() / "mfa-env"


def find_micromamba() -> Optional[Path]:
    """Locate bundled or cached micromamba.exe."""
    root = get_app_root()
    candidates = [
        get_bin_cache_dir() / "micromamba.exe",
        root / "tools" / "micromamba.exe",
    ]
    for p in candidates:
        if p.is_file():
            return p
    return None


def find_mfa_env(*, ignore_external: bool = False) -> Optional[Path]:
    """Locate an existing MFA Python environment.

    Checks `<cache>/mfa-env` first, then (unless `ignore_external` or
    `DIPHONEFORGE_IGNORE_EXTERNAL_MFA=1`) checks repo `tools/mfa-env` and
    `C:\\diphone-mfa-env`.
    """
    candidates = [get_default_mfa_env_dir()]
    env_override = os.environ.get("DIPHONEFORGE_MFA_ENV", "").strip()
    if env_override:
        candidates.insert(0, Path(env_override).expanduser())
    if not ignore_external and os.environ.get("DIPHONEFORGE_IGNORE_EXTERNAL_MFA") != "1":
        candidates.extend([
            get_app_root() / "tools" / "mfa-env",
            Path(r"C:\diphone-mfa-env"),
        ])
    for cand in candidates:
        py = cand / "python.exe"
        if py.is_file():
            return _space_free_path(cand)
    return None


def get_espeak_paths() -> tuple[Path, Path]:
    """Return `(espeak_ng_exe, espeak_data_parent)`."""
    root = get_app_root()
    espeak_dir = root / "tools" / "espeak-ng"
    return espeak_dir / "espeak-ng.exe", espeak_dir


def tool_subprocess_cmd(tool_name: str) -> List[str]:
    """Return subprocess command prefix for an internal pipeline tool in source or frozen-exe mode."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--_run-tool", tool_name]
    return [sys.executable, "-u", str(get_app_root() / "tools" / f"{tool_name}.py")]


def get_mbrolator_binaries() -> Dict[str, Path]:
    """Return paths to the 5 MBROLATOR / MBROLA offline build executables."""
    root = get_app_root()
    tools = root / "tools"
    flat = tools / "mbrolator"
    mapping = {
        "anaf0": (flat / "anaf0.exe", tools / "mbrolator-src" / "AnaMBE" / "Obj" / "anaf0.exe"),
        "anambe": (flat / "anambe.exe", tools / "mbrolator-src" / "AnaMBE" / "Obj" / "anambe.exe"),
        "resynth": (flat / "resynth.exe", tools / "mbrolator-src" / "Resynthesis" / "resynth.exe"),
        "database_build": (flat / "database_build.exe", tools / "mbrolator-src" / "Resynthesis" / "database_build.exe"),
        "mbrola": (flat / "mbrola.exe", tools / "mbrola-src" / "Bin" / "mbrola.exe"),
    }
    return {k: (a if a.is_file() else b) for k, (a, b) in mapping.items()}


def find_preview_engine_dir() -> Optional[Path]:
    """Locate a directory containing 32-bit espeakMbrolaHost.exe + espeak.dll + mbrola.dll + espeak-ng-data."""
    root = get_app_root()
    candidates = [
        root / "engine",
        root / "build" / "stage" / "synthDrivers",
        root / "addon" / "synthDrivers",
    ]
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        candidates.append(Path(appdata) / "nvda" / "addons" / "espeakWitMbrola" / "synthDrivers")
    for cand in candidates:
        if (
            (cand / "espeakMbrolaHost.exe").is_file()
            and (cand / "espeak.dll").is_file()
            and (cand / "mbrola.dll").is_file()
            and (cand / "espeak-ng-data").is_dir()
        ):
            return cand
    return None


def find_nvda_addon_dir() -> Optional[Path]:
    """Locate the installed NVDA espeakWitMbrola add-on synthDrivers folder if present."""
    appdata = os.environ.get("APPDATA", "")
    if not appdata:
        return None
    cand = Path(appdata) / "nvda" / "addons" / "espeakWitMbrola" / "synthDrivers"
    if (cand / "espeak-ng-data").is_dir():
        return cand
    return None


def mfa_subprocess_env(mfa_env_dir: Path) -> Dict[str, str]:
    """Build environment dict for MFA subprocesses with space-free env paths & portable MFA_ROOT_DIR."""
    env_dir = _space_free_path(mfa_env_dir)
    mfa_root = get_mfa_root_dir()
    mfa_root.mkdir(parents=True, exist_ok=True)
    mfa_root_safe = _space_free_path(mfa_root)
    env = os.environ.copy()
    prepend = os.pathsep.join([
        str(env_dir),
        str(env_dir / "Scripts"),
        str(env_dir / "Library" / "bin"),
    ])
    env["PATH"] = prepend + os.pathsep + env.get("PATH", "")
    env["CONDA_PREFIX"] = str(env_dir)
    env["MFA_ROOT_DIR"] = str(mfa_root_safe)
    return env
