#!/usr/bin/env python3
"""Install a custom MBROLA database as a voice for the MBROLA NG NVDA add-on.

MBROLA NG keeps its voices outside the add-on, at
``%APPDATA%\\nvda\\mbrola_ng\\voices\\<id>\\``.  A voice folder contains:

  <id>          the MBROLA database, named exactly like the folder
  <id>.voice    key = value description, including the phoneme map

The add-on's English frontend produces ARPAbet-like phone names.  The default
map translates the ones that differ from this project's inventory:

  A=A:  AI=aI  E=E  EI=eI  O=O  i=i:  u=u:  r==3:
  k_h=k p_h=p t_h=t  @U=oU  {=a

Unlisted names (consonants, @ I U V aU OI ...) pass through unchanged.

The database header is checked against --rate so a 22.05 kHz database cannot be
installed under a 16 kHz label.

Example:
  python tools/install_mbrola_ng_voice.py \
      --database output/mbrola_db/usd16/usd16 --id usd16 --rate 16000 \
      --name "US English custom male (usd16, 16 kHz)"
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import struct
from pathlib import Path

DEFAULT_MAP = (
    "A=A: AI=aI E=E EI=eI O=O i=i: u=u: r==3: "
    "k_h=k p_h=p t_h=t @U=oU {=a"
)


def default_voices_root() -> Path:
    return Path(os.path.expandvars(r"%APPDATA%\nvda\mbrola_ng\voices"))


def database_rate(path: Path) -> int:
    with path.open("rb") as handle:
        header = handle.read(32)
    if header[:6] != b"MBROLA":
        raise SystemExit(f"{path} is not an MBROLA database")
    old = struct.unpack_from("<H", header, 13)[0]
    offset = 19 if old == 0 else 15
    return struct.unpack_from("<h", header, offset + 4)[0]


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 16), b""):
            digest.update(block)
    return digest.hexdigest()


def voice_text(vid: str, name: str, language: str, base_pitch: int, phoneme_map: str) -> str:
    return "\n".join([
        "# MBROLA NG voice description - written by tools/install_mbrola_ng_voice.py",
        f"id = {vid}",
        f"database = {vid}",
        f"language = {language}",
        "version = 1.0",
        "gender = male",
        "age = adult",
        f"base_pitch = {base_pitch}",
        "pitch_range = 1.0",
        "rate_factor = 1.0",
        "volume = 1.0",
        f"phoneme_map = {phoneme_map}",
        f"name_en = {name}",
    ]) + "\n"


def update_registry(root: Path, vid: str, folder: Path, db: Path) -> None:
    path = root / "installed.json"
    registry = {"voices": []}
    if path.exists():
        try:
            registry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            registry = {"voices": []}
    registry["voices"] = [v for v in registry.get("voices", []) if v.get("id") != vid]
    registry["voices"].append({
        "id": vid,
        "language": "en",
        "version": "1.0",
        "path": str(folder),
        "sha256": sha256(db),
        "installed": "2026-10-27T00:00:00Z",
    })
    path.write_text(json.dumps(registry, ensure_ascii=False, indent=1), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", required=True, type=Path)
    parser.add_argument("--id", required=True, help="voice id and folder name, e.g. usd16")
    parser.add_argument("--rate", type=int, required=True, choices=(16000, 22050))
    parser.add_argument("--base-pitch", type=int, default=120)
    parser.add_argument("--language", default="en")
    parser.add_argument("--map", default=DEFAULT_MAP, help="frontend-phone=database-phone ...")
    parser.add_argument("--name", default="", help="name shown in NVDA")
    parser.add_argument("--voices-root", type=Path, default=default_voices_root())
    args = parser.parse_args()

    if not args.database.is_file():
        raise SystemExit(f"database not found: {args.database}")
    rate = database_rate(args.database)
    if rate != args.rate:
        raise SystemExit(f"database is {rate} Hz, but --rate says {args.rate}")

    folder = args.voices_root / args.id
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / args.id
    try:
        shutil.copyfile(args.database, target)
    except PermissionError:
        raise SystemExit(f"{target} is in use; close NVDA or switch away from the voice")
    (folder / (args.id + ".voice")).write_text(
        voice_text(args.id, args.name or args.id, args.language, args.base_pitch, args.map),
        encoding="utf-8",
    )
    update_registry(args.voices_root, args.id, folder, target)
    print(f"installed {args.id} at {target} ({target.stat().st_size} bytes, {rate} Hz)")
    print("restart NVDA, then choose MBROLA NG and the voice " + args.id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
