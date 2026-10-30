# USD16 — provenance

Facts needed to redistribute this database lawfully, and to rebuild it.

## Origin

- **Voice source**: the project's own American English diphone corpus,
  `Voice_audio/`, recorded for this project. No third-party recordings or
  databases were used.
- **Speaker**: one adult male speaker, the project maintainer.
- **Sessions**: see `voice_file_ranges.txt` for the mapping from each source WAV
  to the script lines it contains. The corpus is 1,666 script lines plus a
  46-line coda batch.
- **Format at build time**: 44.1 kHz stereo, mono-mixed and resampled per unit by
  the builder. The database itself is 16 kHz mono, 16-bit signed, MBROLA format.

## Build

- Builder: `tools/build_mbrola_db.py`; see `docs/database/BUILD_REPORT.md`.
- Analysis: `anaf0`, `anambe`, `resynth` and `database_build` from
  `tools/mbrolator-src/`, built locally with w64devkit GCC.
- 2,303 units: 1,073 recorded takes, 94 word-edge units from room tone, and
  1,136 cross-spliced fills that complete the 47x47 pair matrix. Fills are
  listed in `output/mbrola_db/fallback_pairs.csv`.
- Volume ratio 0.8, no clipped samples in validation.

## Rights

The recordings, the derived database and these metadata files are the work of
the project maintainer and are published under Creative Commons Attribution 4.0
International (CC BY 4.0). See `LICENSES/VOICE-DATA.md`.

Attribution string to use:

> USD16 MBROLA voice database, Evangelosz Nagy, CC BY 4.0.

## What is not published

The source recordings in `Voice_audio/` are not part of this repository and no
licence is granted for them. Only the finished database and its metadata are
released. The audit data behind it - unit grid, take selection, validation - is
in `docs/database/`.

## What this licence does not cover

- The MBROLA *engine* is a separate work under the GNU AGPL v3.0. It is not part
  of this voice and is not relicensed here.
- The eSpeak NG descriptor and phone translation data distributed alongside are
  part of eSpeak NG and remain GPL v3.0.
- The MBROLA database *file format* is publicly documented and is not claimed by
  this licence.
