# MBROLA Direction and Voice-Database Plan

This document records the strategic direction chosen for the next phase. It is
intended to make the project resumable without relying on chat history.

## Goal

Build a compact NVDA speech voice using short human recordings, not a large
continuous-speech corpus, a formant synthesizer, or one isolated recording per
phoneme. The desired balance is:

- Human-recorded acoustic material.
- Diphone-scale or similarly short units that preserve phone transitions.
- Explicit duration, pitch, and intonation control from the eSpeak frontend.
- A small binary voice database and a native, low-overhead runtime.
- Practical Windows/NVDA integration.

## Chosen Investigation Path

The first candidate is an **MBROLA-style voice database used with the existing
MBROLA runtime and eSpeak frontend**. This is not an attempt to use an existing
commercial voice or to reverse engineer Doubletalk. It is a way to reuse the
architecture that made old human-recorded synthesizers compact and efficient.

The preferred order is:

1. Create a small custom MBROLA database from the existing recordings.
2. Load it through the installed eSpeak-with-MBROLA NVDA add-on.
3. Compare intelligibility, voice identity, joins, pitch control, and rate
   behavior against the current PSOLA and WORLD outputs.
4. Scale the database only if the small proof of concept is successful.
5. Only write a replacement runtime if MBROLA's quality, speed, licensing, or
   integration limitations become an actual blocker.

## Why MBROLA Fits

MBROLA is a diphone synthesizer, not a text-to-speech frontend. It accepts
phonemes, durations, and piecewise-linear F0 targets. eSpeak can provide those
phonemes and prosodic parameters, which is exactly the behavior desired for
NVDA speech.

The runtime database is a compact binary file containing, in broad terms:

- Database header and metadata.
- Diphone index and names.
- Diphone midpoint/half-segment positions.
- Fixed-period waveform data.
- Compact voiced/unvoiced and transient/stationary frame states.

It is not a directory of ordinary WAV files. The database builder converts
recorded material into fixed-period waveforms before packing it.

Published MBROLA voices are commonly in the several-megabyte range, despite
having roughly 2,000 English diphones. This is the relevant efficiency target.

## Existing NVDA Integration

The installed add-on is:

`C:\Users\vangi\AppData\Roaming\nvda\addons\espeakWitMbrola`

It is version 2.0 and contains:

- `synthDrivers\espeak.dll`
- `synthDrivers\mbrola.dll`
- eSpeak voice descriptors under `synthDrivers\espeak-data\voices\mb\`
- MBROLA phoneme translation data under `synthDrivers\espeak-data\mbrola_ph\`
- MBROLA databases under `synthDrivers\espeak-data\mbrola\`

The add-on loads eSpeak and MBROLA DLLs in-process. It does not launch an
external MBROLA process on Windows. The local driver already streams the
resulting 16-bit PCM to NVDA.

The installed copy's two DLLs are 32-bit, while NVDA 2026 runs 64-bit. That
in-process arrangement therefore fails at `LoadLibrary(espeak.dll)` with
`WinError 193`. A project-local x86-host compatibility prototype now keeps the
existing DLLs in a separate process and streams callback PCM and events to an
x64 NVDA-side module. See `docs/nvda_2026_mbrola_port.md`. The host successfully
synthesized with the installed `mb-en1` database. The user confirmed that the
port now speaks and cancels correctly in NVDA 2026; the runtime compatibility
gate is complete.

A custom voice will normally need:

- One database file under `espeak-data\mbrola\`.
- One voice descriptor, modeled on `mb-us3`.
- One matching eSpeak-to-MBROLA phoneme translation table.

The existing driver should not need to be rewritten for the first experiment.

## Public Toolchain Status

### MBROLA runtime

Repository: `https://github.com/numediart/MBROLA`

- Release 3.3 was published in December 2019.
- The source identifies a newer 3.4 development line.
- The repository received maintenance changes as recently as July 2025.
- Visual Studio projects exist for the standalone program and DLL.
- MinGW and native Windows builds are documented.
- The runtime is old in design but buildable on current Windows with some
  project/toolset adjustments.

### MBROLATOR database builder

Repository: `https://github.com/numediart/MBROLATOR`

MBROLATOR is much older code exposed in a repository in 2019. Its last known
source change was May 2021 and it has no modern release system. Its workflow is:

1. `anaf0` for F0 analysis.
2. `anambe` for multi-band-excitation analysis.
3. `resynth` for MBROLA-compatible fixed-period resynthesis.
4. `database_build` for binary database packing.

The builder expects a segmentation file containing source file, phone pair,
start sample, end sample, and transition sample. It prefers mono, signed
16-bit, little-endian audio at 16 kHz.

The builder's analysis programs use Unix-oriented Makefiles and POSIX calls.
The documentation says Windows use is possible through Cygwin or by generating
a batch file instead of a Makefile. A Linux VM is therefore not mandatory.
Cygwin is likely the least disruptive route for the first build. Native MSVC
porting is possible but is a separate portability task.

The MBROLATOR repository does not have clear top-level license metadata, while
its source files contain AGPL notices. The MBROLA runtime is AGPL-3.0. Review
licensing before distributing an NVDA add-on or database.

## Reuse of This Project

Keep and reuse:

- `Voice_audio/` as the irreplaceable source recordings.
- `voice_file_ranges.txt`.
- `output/segments/` and `output/mfa_aligned/`.
- `output/unit_grid/`, especially phone boundaries and sample coordinates.
- `output/diphone_map.json`, scripts, dictionaries, and metadata.
- `data/phonemes.json` and cluster inventories.
- The recording, segmentation, MFA, and diagnostic tools as offline tools.

The existing MFA alignments can be converted into MBROLATOR `.seg` records.
Do not assume the already extracted short WAV units contain enough analysis
context. The safer conversion should reference the original or line-level
segments and use MFA coordinates to preserve real speech before and after the
diphone.

Do not reuse directly:

- WORLD `.npz` files as the production database format.
- Current PSOLA pitch-mark JSON as MBROLA frame data.
- The current Python/NumPy runtime as the final NVDA engine.

Only selected previously rendered PSOLA and WORLD samples are retained as
comparison baselines; those engines are no longer part of the active project.

## Coverage Strategy

Keep a diphone-first inventory rather than switching to exhaustive CVC or
triphone coverage. The current recordings contain approximately 1,026 unique
diphones across CV, VC, and CCV-derived material. A first database can use this
coverage, then selectively add:

- Word-final and cross-syllable consonant transitions.
- VV transitions if testing shows they matter.
- Important eSpeak allophones such as flaps and aspiration.
- Additional VC donors for approximants.

CCV prompts should normally contribute their adjacent diphones to an MBROLA
database. They are contextual candidates, not a new native three-phone unit
type.

Prompt count and unit count are not the same thing. A `C1 C2 V` prompt yields
`C1_C2` (new) and `C2_V` (already covered by the CV inventory). In the current
recordings, the 621 CCV prompts (27 onset clusters x 23 vowels) produced 1,242
units but only **27 new labels**; 1,215 of those units duplicate labels already
present in CV. One prompt per onset cluster would have produced the same 27
labels. The extra vowels are alternate takes, useful for picking the best CC
recording but not required for coverage. The same applies to VCC codas: one
prompt per cluster is enough, because only the `C1_C2` label is new.

A lean recording plan for a future voice is therefore:

- CV: one prompt per consonant-vowel pair (the core matrix).
- VC: one prompt per vowel-consonant pair, minus r-coloured vowel duplicates.
- CC: one prompt per onset and coda cluster, not one per vowel.
- VV: only the pairs that the frontend actually requests, not the Cartesian set.
- `_C` and `C_`: derive from room tone; no separate recording.

For the current phone set that is about 1,026 prompts for the same 1,026 unique
labels, before adding coda clusters, VV pairs, and derived boundary units.
Multiple takes per cluster remain useful for quality selection, but two or
three are enough; twenty-three is not necessary.

## Staged Feasibility Plan

### Stage 1: runtime smoke test

Use the existing add-on and a known database to confirm the exact local voice
directory, descriptor, translation-table, and database-loading behavior.

### Stage 2: tiny custom database

Select enough existing material for a few test words such as `see`, `tap`, and
`pat`. Generate source audio with adequate context, a `.seg` file, MBROLATOR
analysis outputs, and a packed database. Load it through the existing add-on.

### Stage 3: quality gate

Compare the custom voice with PSOLA and WORLD at normal speed. The gate is:

- Clearly intelligible output.
- Audible identity from the recorded speaker.
- No worse joins than the current baseline.
- Pitch and eSpeak intonation remain usable.
- Database size and runtime cost are meaningfully smaller.

### Stage 4: full inventory

Process the complete usable diphone inventory only after Stage 3 passes. Add
missing coverage based on actual listening failures, not theoretical Cartesian
products.

### Stage 5: fast-rate work

Treat extreme screen-reader rate as a separate optimization. Start with eSpeak
duration scaling and MBROLA's existing time manipulation. If necessary, add
special duration rules that preserve stressed vowel nuclei and shorten less
important consonant material. Do not record a second fast voice until normal
speed quality is established.

## Decision Rule: Reuse or Rewrite

Do not write a new runtime before testing the existing one. The expected risk is
reversed from the current project:

- Runtime integration: low risk.
- Binary database packing: moderate risk.
- Legacy MBE analysis portability: moderate-to-high risk.
- Final voice quality: uncertain and must be auditioned.

If MBROLATOR analysis is the blocker, first port or replace only the offline
analysis/database-building step while retaining the MBROLA runtime and `.pho`
contract. A full custom engine becomes justified only if the resulting voice
quality or performance is unacceptable.

## References

- MBROLA: `https://github.com/numediart/MBROLA`
- MBROLATOR: `https://github.com/numediart/MBROLATOR`
- MBROLA database structure: `https://github.com/numediart/MBROLA/blob/master/Database/database.h`
- MBROLA ROM/database notes: `https://github.com/numediart/MBROLA/blob/master/Documentation/rom_documentation.txt`
- eSpeak-NG MBROLA integration: `https://github.com/espeak-ng/espeak-ng/blob/master/docs/mbrola.md`
- Local NVDA bridge: `C:\Users\vangi\AppData\Roaming\nvda\addons\espeakWitMbrola`
