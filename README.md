# DiphoneForge

A voice builder and an NVDA add-on for MBROLA.

This repository has two halves that share one purpose: a usable MBROLA voice in
NVDA today, and the tooling to make more MBROLA voices from human recordings
tomorrow.

1. **The NVDA add-on.** A 64-bit eSpeak NG synthesizer that drives MBROLA voices
   through a 32-bit helper process, shipped with one complete voice built from
   this project's own recordings.
2. **The voice builder.** The scripts that turn a prompt list and a set of
   recordings into a finished MBROLA database, including a matching eSpeak voice
   descriptor and phone translation table. It is currently a collection of
   command-line stages; turning it into a guided, interactive workflow is the
   next major piece of work.

The long-term goal is that someone with a microphone, or with recordings made
elsewhere, can generate a recording script for their language, import what they
recorded, and end up with a voice that NVDA can use, without needing to
understand MBROLA's internals or ask anyone for help.

## Status

| Component | State |
| --- | --- |
| NVDA add-on, eSpeak NG 1.51 + MBROLA, 64-bit | **Complete and in daily use** |
| USD16 voice database, 16 kHz | **Complete**, accepted on listening |
| USD voice database, 22.05 kHz | **Complete**, proof of concept for higher rates |
| Voice builder as a pipeline of stages | Working scripts, project-specific, English only |
| Voice builder as a guided application | Not started; design in `docs/voice_builder_plan.md` |
| Additional languages | Not started |
| MBROLA NG frontend | Separate add-on, experimental |

## What the add-on gives you

- eSpeak NG for phonemes, timing and pitch, MBROLA for the audio.
- Speech that stays intelligible at very fast rates and responds immediately to
  navigation, because audio streams in 20 ms blocks while the rest of the
  utterance is still being synthesised.
- Index marks and interruption that behave correctly: marks arrive when their
  audio plays, an interrupted utterance never resumes halfway, and queued
  utterances run back to back.
- Two voices built here: **USD16** (16 kHz, the default) and **USD** (22.05 kHz,
  the same recordings at a higher sample rate).

## Requirements

NVDA 2026.1 or later, 64-bit. The add-on is built against that API because the
64-bit to 32-bit bridge relies on it. The manifest declares
`minimumNVDAVersion = 2026.1.0`, so older NVDA releases will not offer it.

## Install

Download the latest `espeakWitMbrola-<version>.nvda-addon` from the Releases
page, open it, confirm the installation, and restart NVDA. Then choose
**eSpeak NG with MBROLA** in NVDA's synthesizer list and pick a voice.

The add-on keeps its original identifier, `espeakWitMbrola`, so existing
configurations and the upstream project's identity carry over. The repository
name describes the larger project; the add-on name does not.

## Other MBROLA voices are supported

The add-on ships one voice, but nothing in it is limited to that voice. Any
MBROLA database works:

1. Download a database from
   [numediart/MBROLA-voices](https://github.com/numediart/MBROLA-voices).
2. Copy it into `espeak-ng-data\mbrola` in the add-on folder inside your NVDA
   configuration directory.
3. Restart NVDA. The voice appears in the list.

Only databases that are actually installed are listed, so a missing voice is
never offered and never produces an error. eSpeak already carries the
descriptors and phone translation tables for the standard MBROLA voices, so no
extra configuration is needed. Those databases keep their own licences: they may
be copied freely when no charge is made, they may not be sold or bundled into a
sold product without their author's permission, and they may be used only with
MBROLA. They are deliberately **not** included here, so this repository ships
only material it has the right to ship.

## Repository layout

```
addon/                  the NVDA add-on source (nothing else is packaged)
  manifest.ini
  installTasks.py       keeps user-added voices across updates
  synthDrivers/         driver, bridge, host source
  doc/en/readme.html    the documentation NVDA shows
voices/                 voice packages, one folder per voice
  usd16/  usd/          database, descriptor, phtrans, metadata, provenance
fixtures/poc/           eleven-unit reference build used to test the toolchain
docs/database/          audit data for the shipped voice: unit grid, take
                        selection, validation, build report
build.ps1               assembles dist/<name>-<version>.nvda-addon
build/, dist/           generated, not committed
data/                   phoneme inventories and cluster definitions
docs/                   design, recording guide, port notes, builder plan
tools/                  voice builder scripts and native toolchain sources
LICENSES/               full licence texts
NOTICE.md               third-party components, versions and terms
```

The add-on is deliberately self-contained: `build.ps1` copies `addon/`, adds the
third-party engines and the voice packages, and writes a single `.nvda-addon`
file. Nothing from `tools/` or `docs/` ends up in it.

## What is not in this repository

Two directories exist on the maintainer's machine and are excluded by
`.gitignore`:

- **`Voice_audio/`** — the original recordings, 493 MB across 19 files. These
  are not published, and no licence is granted for them. A finished diphone
  database can only speak through MBROLA; the master recordings can be turned
  into any kind of voice, including ones nobody consented to. They also contain
  retakes and material between takes that is not part of the corpus. If you need
  the corpus for research, ask.
- **`output/`** — the working directory: per-line clips, alignments, per-unit
  audio, build intermediates and a second copy of the databases. About 300 MB,
  all derived, most of it regenerable.

What those directories contain that is worth publishing has been copied out:
the audit data is in `docs/database/`, the reference build is in `fixtures/poc/`,
and the databases themselves are in `voices/`. The voice packages are therefore
complete and self-contained; the pipeline that produced them is documented but
not reproducible byte for byte from this repository, which is normal for voice
data.

## Building the add-on

Prerequisites:

- Windows with a MinGW-w64 toolchain that can target 32-bit (`g++ -m32`,
  `gcc -m32`). w64devkit works.
- A source for the third-party engines: either an existing installation of this
  add-on, or the official eSpeak NG 1.51 32-bit build plus the MBROLA binaries.
  They are not committed here, and the build script does not download them.

```powershell
.\build.ps1                     # compiles the host and the streaming engine
.\build.ps1 -SkipCompile        # reuses existing binaries from build\bin
.\build.ps1 -VendorSource "C:\path\to\espeak-ng-engine"
```

The result is `dist\espeakWitMbrola-<version>.nvda-addon`. A build with the
toolchain available produces both native binaries from source, which is what
keeps the GPL and AGPL source obligations satisfied. `tools\compare-install.ps1`
checks an installed add-on against `build\stage` file by file.

## Building a voice

The current pipeline, in the order it runs:

| Stage | Script | Output |
| --- | --- | --- |
| Plan the corpus | `tools/generate_recording_script.py` | prompts, transcripts, diphone map |
| Record | (any recorder) | WAV files plus a line-range file |
| Segment and align | `tools/make_mfa_corpus.py`, `tools/build_mfa_dictionary.py`, `tools/resegment_from_whole_alignment.py` | per-line clips, TextGrids |
| Build the unit grid | `tools/build_unit_grid.py` | diphone labels and boundaries |
| Audit | `tools/audit_units.py`, `tools/analyze_unit_grid.py` | quality reports, pickup lists |
| Build the database | `tools/build_mbrola_db.py` | MBROLA database, manifest, reports |
| Package the voice | `tools/make_espeak_voice.py`, the `voices/` layout | descriptor, phtrans, `.voice` |

Three things a new user has to do by hand today, and that the guided builder is
meant to remove: deciding how many prompts to record, mapping recorded files to
script lines, and judging which takes are worth keeping. Everything else is
automatic and rule-based; no AI assistance is involved or planned.

## The reference build

`fixtures/poc/` is an eleven-unit database (`_s`, `s_i:`, `i:_`, `_t`, `t_a`,
`a_p`, `p_`, `_p`, `p_a`, `a_t`, `t_`) together with the segmentation file,
the per-unit audio and the four test utterances it can speak. It is 320 KB and
is the fixture any change to the analysis or packing stages must keep working.
It is also the best way to see what the builder actually consumes.

## Licensing in one paragraph

The add-on code is GPL v2.0, inherited from NVDA's eSpeak driver. The MBROLA
engine is AGPL v3.0, and the streaming engine here is built from that source
with one small patch, which is in this repository. eSpeak NG and its data are
GPL v3.0. The two voice databases are published under CC BY 4.0, separately from
the code. Third-party MBROLA databases, and the source recordings, are not
distributed here at all. Full details: `LICENSE`, `LICENSES/`, and `NOTICE.md`.

## Credits

This work stands on several earlier projects, and most of the add-on's
behaviour is not new:

- **Felipe Porciuncula Zanabria** wrote the original "Espeak with Mbrola
  included" NVDA add-on (GPL v2.0), which ported classic eSpeak with MBROLA
  support into an add-on and fixed the sample-rate handling that made those
  voices sound wrong. This repository is a continuation of that work.
- **NV Access Limited and the NVDA contributors**, in particular the eSpeak
  synthesizer driver, which this driver descends from.
- **Jonathan Duddington** and the **eSpeak NG contributors**, whose engine
  provides the phonemes, timing model and pitch.
- **The TCTS Laboratory of the University of Mons** and the **numediart
  institute**, for MBROLA and MBROLATOR, and for keeping both available as free
  software.
- The authors of the individual MBROLA voices this add-on can drive.

## Contributing

Issues and pull requests are welcome, especially reports about specific voices,
rates or NVDA configurations. The voice builder is the part that most needs
hands: a second language is worth more than any amount of refactoring.
