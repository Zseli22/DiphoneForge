# USD — provenance

Facts needed to redistribute this database lawfully, and to rebuild it.

## Origin

- **Voice source**: the project's own American English diphone corpus,
  `Voice_audio/`. No third-party recordings or databases were used.
- **Speaker**: one adult male speaker, the project maintainer.
- **Relationship to USD16**: this is the same 2,303 units from the same corpus,
  built at 22.05 kHz instead of 16 kHz. It is not a resampled copy of `usd16`;
  the analysis and resynthesis ran at the higher rate.
- **Format**: 22.05 kHz mono, 16-bit signed, MBROLA format.

## Build

- Builder: `tools/build_mbrola_db.py`; see `docs/database/BUILD_REPORT.md`.
- At 22.05 kHz the analysis still uses the 16 kHz parameter files; only the rate
  passed to `database_build` changes (period 180 instead of 120). This is the
  documented MBROLATOR workaround and reproduces the accepted build.
- eSpeak 1.48 and eSpeak NG both assume 16 kHz unless the voice descriptor's
  `mbrola` line carries a third field, which is why `mb-usd` reads
  `mbrola usd usd_phtrans 22050`.

## Rights

The recordings, the derived database and these metadata files are the work of
the project maintainer and are published under Creative Commons Attribution 4.0
International (CC BY 4.0). See `LICENSES/VOICE-DATA.md`.

Attribution string to use:

> USD MBROLA voice database (22.05 kHz), Evangelosz Nagy, CC BY 4.0.

## What is not published

The source recordings in `Voice_audio/` are not part of this repository and no
licence is granted for them. Only the finished database and its metadata are
released. The audit data behind it - unit grid, take selection, validation - is
in `docs/database/`.

## What this licence does not cover

- The MBROLA engine (AGPL v3.0), the eSpeak NG descriptor and phone translation
  data (GPL v3.0), and the published MBROLA database format are separate works
  and keep their own terms.
