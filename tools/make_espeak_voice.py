#!/usr/bin/env python3
"""Write an eSpeak voice descriptor and phone table for the custom database.

eSpeak's MBROLA interface passes phoneme names to MBROLA, but several of the
names it uses are not the ones in the recorded inventory, and an unmapped name
is replaced by silence (with the phone's duration).  The important cases are:

  l/2   the coda/dark /l/ sent by eSpeak (the mnemonic display shows "l")
  @L    syllabic /l/ in "table", "little", "people"
  aI3   r-coloured /aI/ in "fire", "require", "tired"
  aU@   r-coloured /aU/ in "hour", "sour", "flour"
  aU3   r-coloured /aU/ in "flower", "power", "shower"
  eI3   r-coloured /eI/ in "player", "layer"
  OI3   r-coloured /OI/ in "employer"
  i@3   r-coloured /i@/ in "near", "here"

The translation table maps those to the recorded phones.  A record may produce
two phones (for example aI3 -> aI + r).  When eSpeak appends a linking /r/
before a following vowel ("fire is" -> aI3 r- I z) a conditional record maps the
same source to its first phone only, so the r is not spoken twice.

The phtrans format is a 4-byte header followed by 24-byte records.  Each record
is six 4-byte fields, all little-endian: source name, next-phoneme condition,
first output phone, second output phone, split percentage, control bits.  Phone
names are packed as up to four ASCII bytes, not NUL-padded eight-byte strings.
The split percentage is the share of the source phoneme's duration given to the
first output phone; eSpeak only emits the second phone when the source is a
vowel, which is the case for all the two-phone entries here.
"""

from __future__ import annotations

import argparse
import json
import struct
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PHONEMES = PROJECT_ROOT / "data" / "phonemes.json"

#: Value written into the table header, in sixteenths.  eSpeak loads it as
#: volume/16, so 20 gives the 1.25 gain that en1 uses and that the database
#: build's 0.8 volume ratio is calibrated for.
PHTRANS_HEADER = 20
PHTRANS_RECORD = 24
MAX_NAME_BYTES = 4

#: Share of the source phoneme's duration given to the first output phone when
#: a record produces two phones.  The second output phone gets the remainder.
#: The values keep the vowel/diphthong long and give the closing phone the
#: shorter tail, as en1 does for its own aI@ and aU@ records.
SPLITS: dict[str, int] = {
    "@L": 50,      # syllabic l: schwa + l
    "aI3": 70,     # rhotic aI: aI + r
    "aU@": 70,     # rhotic aU: aU + r
    "aU3": 70,     # rhotic aU: aU + r
    "eI3": 70,     # rhotic eI: eI + r
    "OI3": 70,     # rhotic OI: OI + r
    "i@3": 70,     # rhotic i@: i@ + r
}

#: Sources that eSpeak may follow with an explicit linking /r/.  For these the
#: table gets conditional records that drop the second phone when the next
#: phoneme already is r.
RHOTIC: tuple[str, ...] = ("aI3", "aU@", "aU3", "eI3", "OI3", "i@3")

#: Mapping record: source, next-phoneme condition, output phones, split.
Record = tuple[str, str, tuple[str, ...], int]

#: Mappings that need a different output than the single-target table in
#: data/phonemes.json (or that need two phones).  Verified against the
#: installed eSpeak 1.48 build.
OVERRIDES: dict[str, tuple[str, ...]] = {
    "@L": ("@", "l"),      # syllabic l: schwa + l, both recorded
    "l/2": ("l",),         # coda l; without this the whole l is silent
    "L": ("l",),
    "l/": ("l",),
    "l/3": ("l",),
    "l^": ("l",),
    "l.": ("l",),
    "l-": ("l",),
    "L/": ("l",),
    "aI3": ("aI", "r"),    # r-coloured aI; aI_r is a recorded pair
    "aU@": ("aU", "r"),    # r-coloured aU; aU_r is a recorded pair
    "aU3": ("aU", "r"),    # linking-r form of aU@ ("power", "shower")
    "eI3": ("eI", "r"),    # r-coloured eI ("player")
    "OI3": ("OI", "r"),    # r-coloured OI ("employer")
    "i@3": ("i@", "r"),    # r-coloured i@ ("near", "here")
    "aI2": ("aI",),
    "a/": ("a",),
    "x": ("k",),         # Scottish loch; not used by en-us
    "IR": ("3:",),       # British r-coloured vowels, mapped for safety
    "VR": ("3:",),
}


def load_mapping() -> dict[str, tuple[str, ...]]:
    spec = json.loads(PHONEMES.read_text(encoding="utf-8"))
    canonical = set(spec["vowels"]) | set(spec["consonants"])
    mapping: dict[str, tuple[str, ...]] = {}
    for source, target in spec["espeak_variants"].items():
        if source == "_comment":
            continue
        if target and target not in canonical:
            raise ValueError(f"{source}: target {target!r} is not a canonical phone")
        if source in canonical:
            continue
        mapping[source] = (target,) if target else ()
    mapping.update(OVERRIDES)
    for source, outputs in mapping.items():
        for output in outputs:
            if output not in canonical:
                raise ValueError(f"{source}: output {output!r} is not a canonical phone")
    return mapping


def pack_name(name: str) -> bytes:
    encoded = name.encode("ascii")
    if len(encoded) > MAX_NAME_BYTES:
        raise ValueError(f"phone name {name!r} is longer than {MAX_NAME_BYTES} bytes")
    return encoded.ljust(MAX_NAME_BYTES, b"\0")


def build_records(mapping: dict[str, tuple[str, ...]]) -> list[Record]:
    records: list[Record] = []
    for source in sorted(mapping):
        outputs = mapping[source]
        if len(outputs) > 2:
            raise ValueError(f"{source}: a record holds at most two output phones")
        if len(outputs) == 2 and source not in SPLITS:
            raise ValueError(f"{source}: a two-phone record needs a split percentage")
        if source in RHOTIC:
            # Before a following vowel eSpeak appends r- or r itself.  Use the
            # first phone alone then, so the coda is spoken once.
            for next_phone in ("r-", "r"):
                records.append((source, next_phone, (outputs[0],), 0))
        records.append((source, "", outputs, SPLITS.get(source, 0)))
    return records


def phtrans_bytes(records: list[Record]) -> bytes:
    out = bytearray(struct.pack("<I", PHTRANS_HEADER))
    for source, next_phone, outputs, percent in records:
        first = outputs[0] if outputs else ""
        second = outputs[1] if len(outputs) == 2 else ""
        out += pack_name(source)
        out += pack_name(next_phone)
        out += pack_name(first)
        out += pack_name(second)
        out += struct.pack("<i", percent)
        out += struct.pack("<i", 0)             # no control bits
    return bytes(out)


def decode_phtrans(data: bytes) -> list[Record]:
    """Read a table back using the engine's layout, for verification."""
    records: list[Record] = []
    for offset in range(4, len(data), PHTRANS_RECORD):
        record = data[offset:offset + PHTRANS_RECORD]
        names = [
            record[start:start + MAX_NAME_BYTES].rstrip(b"\0").decode("ascii")
            for start in (0, 4, 8, 12)
        ]
        percent = struct.unpack("<i", record[16:20])[0]
        outputs = tuple(name for name in names[2:] if name)
        records.append((names[0], names[1], outputs, percent))
    return records


def voice_text(name: str, db_name: str, table_name: str, pitch_low: int, pitch_high: int,
               sample_rate: int = 0) -> str:
    rate_field = f" {sample_rate}" if sample_rate > 0 else ""
    return (
        f"name {name}\n"
        "language en-us\n"
        "language en 2\n"
        "gender male\n"
        "\n"
        "phonemes en-us\n"
        f"pitch {pitch_low} {pitch_high}\n"
        f"mbrola {db_name} {table_name}{rate_field}\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", default="us-mbrola-usd")
    parser.add_argument("--database", default="usd")
    parser.add_argument("--table", default="usd_phtrans")
    parser.add_argument("--pitch", default="82,117")
    parser.add_argument("--rate", type=int, default=0,
                        help="database sample rate written as the optional third "
                             "field of the mbrola line. eSpeak 1.48 assumes 16000 "
                             "when the field is absent, whatever the database says, "
                             "so a 22.05 kHz database needs --rate 22050.")
    parser.add_argument("--voice-file", default="",
                        help="voice file name; defaults to the database name. "
                             "eSpeak 1.48 ignores new files in espeak-data/voices/mb, "
                             "so install the descriptor in the voices root.")
    parser.add_argument("--out", default=str(PROJECT_ROOT / "output" / "mbrola_db" / "espeak_voice"))
    args = parser.parse_args()

    low, high = (int(value) for value in args.pitch.split(","))
    mapping = load_mapping()
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    table_path = out_dir / args.table
    records = build_records(mapping)
    data = phtrans_bytes(records)
    table_path.write_bytes(data)
    voice_path = out_dir / (args.voice_file or args.database)
    voice_path.write_bytes(voice_text(args.name, args.database, args.table, low, high,
                                      args.rate).encode("ascii"))

    if decode_phtrans(data) != records:
        raise ValueError("table did not read back as written")

    print(f"wrote {table_path} ({len(records)} records, {len(mapping)} sources, {len(data)} bytes)")
    print(f"wrote {voice_path}")
    for source, next_phone, outputs, percent in records:
        condition = f" before {next_phone!r}" if next_phone else ""
        split = f" ({percent}/{100 - percent})" if len(outputs) == 2 else ""
        spoken = " + ".join(outputs) if outputs else "(silence)"
        print(f"  {source!r}{condition} -> {spoken}{split}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
