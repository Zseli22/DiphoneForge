# DiphoneForge — Working Context

## Goal & Current Focus

DiphoneForge has two parts:
1. **The NVDA add-on (`espeakWitMbrola`):** **Complete and stable** (release `v3.0`,
   published at `https://github.com/Zseli22/DiphoneForge`). Drives MBROLA voices on
   64-bit NVDA 2026.1+ via a 32-bit eSpeak NG 1.51 + `mbrola_stream.dll` host,
   shipping `usd16` (16 kHz, default) and `usd` (22.05 kHz, POC). Maintenance only.
2. **The reusable voice builder (active focus):** Turn the project-specific English
   scripts into a language- and speaker-neutral pipeline so anyone with recording
   equipment can build an MBROLA voice for their language.

## Session Rules

- **Never commit or push unless explicitly requested.**
- **Never commit `Voice_audio/`, `output/`, `history.md`, or third-party MBROLA
  databases.** Publish derived material only through `voices/`, `docs/database/`,
  or `fixtures/poc/`.
- **Keep original recordings unchanged** (`Voice_audio/` is read-only source).
- **Preserve the `segments.csv` (`source_start_s`) to TextGrid time mapping** when
  modifying alignments.
- **Keep `addon/` self-contained** — no builder code, corpora, or generated data
  ever enter the packaged `.nvda-addon`.
- **Update `NOTICE.md`** whenever adding a dependency or vendored source.
- **Do not treat `tools/mfa-env` as a normal project directory** — it is a junction
  to `C:\diphone-mfa-env`.
- Keep this file concise (~150 lines max). Put long design notes in `docs/` and
  local build history in git-ignored `history.md`.

## Repository Layout

```
addon/              NVDA add-on source (manifest.ini, installTasks.py, synthDrivers/, doc/)
voices/             Published voice packages (usd16/, usd/: DB, descriptor, phtrans, .voice, PROVENANCE.md)
data/               US English phoneme inventory and onset/coda cluster definitions
docs/               Design notes, recording guide, publishing guide, database/ audit data
fixtures/poc/       11-unit reference build (320 KB) — toolchain regression fixture
tools/              Pipeline scripts, add-on smoke tests, vendored mbrola-src/ & mbrolator-src/
  patches/          Diffs against vendored upstream commits (see tools/VENDORED.md)
LICENSES/ NOTICE.md Split-licence documentation (GPL-2.0, GPL-3.0, AGPL-3.0, CC BY 4.0)
build.ps1           Assembles dist/espeakWitMbrola-<version>.nvda-addon
-- local-only, never committed --
Voice_audio/        Original 44.1 kHz stereo recordings (493 MB, read-only)
output/             Working directory: segments, alignments, unit grid, DB intermediates
history.md          Archived build/debugging log (git-ignored)
```

## Critical Technical Invariants

### NVDA Add-on & Engine
- 64-bit NVDA runs `_espeak_mb.py` / `espeakwithmbrola.py`; 32-bit
  `espeakMbrolaHost.exe` loads 32-bit eSpeak NG 1.51 (`espeak.dll`) + `mbrola.dll`
  and streams 20 ms PCM blocks via `mbrola_stream.dll` (set `ESPEAK_MB_MBROLA_EXE=1`
  for the bit-identical `mbrola.exe` fallback).
- NVDA's own bundled eSpeak DLL is built **without** MBROLA support; the separate
  32-bit engine is required.
- Host ignores `espeak_SetParameter` `EINVAL` on NG params 5, 6, 8; generates `.pho`
  at 175 wpm and applies duration floors (25 ms vowel, 15 ms consonant, 8 ms pause)
  plus 2.4x soft-knee limiter gain (`ESPEAK_MB_GAIN` overrides).
- Build add-on: `.\build.ps1` (needs 32-bit MinGW `g++`/`gcc` and `-VendorSource` or
  installed add-on). Verify with `.\tools\compare-install.ps1` and
  `tools\smoke_mbrola_host.py`. See `docs/publishing.md`.
- `installTasks.py` preserves user-added voices across add-on upgrades without
  overwriting shipped files.

### Databases, eSpeak Voice Tables & Licensing
- `usd16` (16 kHz, 15.6 MB) and `usd` (22.05 kHz, 21.6 MB) each have 2,303 units
  (1,073 recorded + 94 room-tone word edges + 1,136 cross-spliced fills) completing
  the 47x47 matrix, because MBROLA aborts on any missing diphone.
- Voice descriptors **must** carry the sample rate on the `mbrola` line for non-16 kHz
  voices (`mbrola usd usd_phtrans 22050`), or eSpeak assumes 16 kHz.
- `phtrans` binary records (`tools/make_espeak_voice.py`) are 4-byte header + 24-byte
  records: six 4-byte little-endian fields (source, next-phone condition, out1, out2,
  split %, control bits), names packed up to 4 ASCII bytes.
- Split licensing: add-on Python/host GPL-2.0; eSpeak NG GPL-3.0+; MBROLA /
  `mbrola_stream.dll` AGPL-3.0+ (must ship corresponding source & build); `usd16`/`usd`
  CC BY 4.0; third-party MBROLA databases are supported but never distributed.

### Offline MBROLATOR Toolchain (Windows / w64devkit GCC 15.2)
Built from `tools/mbrolator-src/` and `tools/mbrola-src/` (see `tools/VENDORED.md`):
- **AnaMBE (`anaf0.exe`, `anambe.exe`):** MUST compile with `-DLITTLE_ENDIAN`.
- **`resynth.exe`:** MUST open `.s16`/`.pm` in `"wb"` mode and compile with
  `-O0 -fno-strict-aliasing -include Src/wincompat.h` (`-O2` miscompiles harmonic
  matching into NaN/silence and runs ~200x slower).
- **`database_build.exe`:** Windows-safe `date`/`rm` guards + `wincompat.c` (`drand48`);
  volume ratio `0.8` prevents isolated clipping.
- **22.05 kHz analysis rule:** Keep `Freq=16000` in `.f0`/`.mbe`/`.syn`; scale
  `FrameLength`/`FrameShift`/period to the speaker's F0 at the target rate, and pass
  the real sample rate (`22050`) only to `database_build`.
- **`mbrola_stream.dll`:** Needs `close_InputFifo` non-static in `Parser/input_fifo.c`.
- **MFA env:** Recreate with
  `tools\micromamba.exe create -p C:\diphone-mfa-env -c conda-forge --file tools\mfa-packages.txt`.

## Active Direction & Next Work

1. **Hybrid real-word + minimal-pair prompt builder & eSpeak language bootstrapper:**
   Use `espeak-ng` + a frequency word list to discover a language's actual emitted
   phones, real-text diphones, and IPA mapping; select a compact greedy set-cover of
   short real words (covering most transitions naturally in far less recording time),
   supplemented only at the end by a small tail of nonsense pairs for rare gaps.
2. **Pilot on UK English (`en-gb`):**
   Test the new real-word prompt generation, alignment, and segmentation workflow by
   building a UK English voice alongside `usd16` before expanding to additional
   languages (e.g., Hungarian, Spanish, Croatian).
3. **Unified, speaker- & language-neutral voice builder (`project.json`):**
   - Consolidate stage scripts into a clean CLI/package with idempotent stages
     (`init`, `plan`, `import` / `record`, `align`, `grid`, `audit`, `build`, `package`).
   - Support **both** recording workflows into the same per-line clip format:
     - **External recorder import** (long WAVs + strict range-map validation: every
       prompt once, no gaps, no overlaps).
     - **Built-in keyboard-accessible prompt recorder** (for users with a good USB/XLR
       mic: play ref → record → instant clipping/SNR check → accept/retake).
   - Auto-estimate speaker median F0 to derive `period`, `FrameShift`, `FrameLength`,
     and descriptor pitch range (remove hardcoded 122 Hz male constants in
     `tools/build_mbrola_db.py`).
   - Derive articulatory donor distances from IPA instead of hardcoded English tables,
     and put forced alignment behind a pluggable interface (supporting IPA-based MFA
     models and lighter CTC aligners).
   - Automate regression tests against `fixtures/poc/`.

## Key References

- Overview & user install: `README.md`
- Licensing & third-party sources: `NOTICE.md`, `tools/VENDORED.md`
- Builder design & recording guide: `docs/voice_builder_plan.md`, `docs/recording_guide.md`
- Release process & repo layers: `docs/publishing.md`, `docs/project_layout.md`
- Shipped voice audit & build report: `docs/database/BUILD_REPORT.md`
- Archived build/debug history (local only): `history.md`
