# DiphoneForge — Working Context

## Goal

Build a US English voice from human recordings as an MBROLA database, drive it in
NVDA through an eSpeak NG frontend, and turn the one-off pipeline into a
reusable voice builder that anyone can run for their own recordings and
language. The NVDA add-on is the first product; the builder is the long-term
one.

## Current state (2026-10-30)

The add-on is **complete and stable**. Daily use confirms it speaks correctly at
normal and very fast rates, with rate boost on and off, and that index marks,
interruption and queued utterances all behave.

- NVDA 2026 x64 bridge works. The add-on requires NVDA 2026.1 or later because
  the 64-bit to 32-bit bridge was built against that API.
- eSpeak NG 1.51 drives a 32-bit host and the existing 32-bit `mbrola.dll`. The
  classic eSpeak 1.48 path has been removed; nothing depends on it.
- Two databases exist and are accepted:
  - `output/mbrola_db/usd16/usd16` — 16 kHz, 2,303 units, shipped as the
    default voice.
  - `output/mbrola_db/usd/usd` — 22.05 kHz, same units rebuilt at the higher
    rate, shipped as a proof of concept for higher-fidelity databases.
- The 16 kHz build passes end-to-end synthesis, including a 54-minute text run
  with no missing diphones.
- Listening verdict (2026-10-27): the eSpeak NG `.pho` timing engine and the
  custom voice sound correct, rate scales cleanly, and stock MBROLA voices also
  improve.
- The add-on now ships only these two voices. Third-party MBROLA databases are
  supported but not distributed; see "Licensing" below.
- Published at `https://github.com/Zseli22/DiphoneForge`. Release `v3.0` carries
  `espeakWitMbrola-3.0.nvda-addon` and its SHA256.
- The voice builder is still a set of project-specific scripts, English only.
  Making it a guided, language-neutral workflow is the active development goal.

## Repository layout

```
README.md  LICENSE  NOTICE.md  LICENSES/  build.ps1
addon/                  add-on source, nothing else is packaged
  manifest.ini  installTasks.py
  synthDrivers/         _espeak_mb.py, espeakwithmbrola.py, espeak_mbrola_host.cpp
  doc/en/readme.html
voices/                 voice packages: database, descriptor, phtrans, .voice,
                        PROVENANCE.md
  usd16/  usd/
build.ps1               addon/ + vendor engines + voices -> dist/*.nvda-addon
build/  dist/           generated, git-ignored
data/                   phoneme inventories, onset and coda cluster definitions
docs/                   design notes, recording guide, port notes, builder plan
docs/database/          published audit data: unit grid, takes, validation
fixtures/poc/           eleven-unit reference build for toolchain checks
tools/                  pipeline scripts and the native toolchain sources
  patches/              diffs against the vendored upstream commits
  VENDORED.md           upstream URLs, commits and local changes
-- private, excluded by .gitignore, never committed --
output/                 working directory: line clips, alignments, per-unit
                        audio, intermediates, a second copy of the databases
Voice_audio/            original recordings, unmodified, 493 MB
```

`addon/` must stay self-contained: `build.ps1` copies it, adds the third-party
engines and the voice packages, and zips the result. No builder code, corpus or
output ever enters the package.

The repository is named `DiphoneForge`; the add-on keeps its identifier
`espeakWitMbrola` for continuity with existing configurations and with the
upstream add-on's identity. `Voice_audio/` and `output/` are private working data
and never enter the repository: the finished database is published, the
recordings and their audio derivatives are not. Anything from `output/` that
belongs in public view has a home already — `docs/database/` for audit data,
`fixtures/poc/` for the reference build — so new material goes there rather than
being committed in place.

## Licensing and third-party material

Decided 2026-10-30. The repository is source-available and free; the add-on
ships only material this project can license.

| Material | Licence | Notes |
| --- | --- | --- |
| Add-on Python driver, host, `installTasks.py` | GPL-2.0 | Descends from NVDA's eSpeak driver and Felipe Porciuncula Zanabria's add-on 2.0. Keep both copyright lines; mark changes. |
| eSpeak NG 1.51 engine and data | GPL-3.0-or-later | Redistributed unmodified. The br/es MBROLA phone tables carry Felipe's corrections. |
| MBROLA runtime, `mbrola_stream.dll` | AGPL-3.0-or-later | The DLL is a local build of `tools/mbrola-src` with the `close_InputFifo` patch, so the patch and build command must stay published. |
| `usd16`, `usd` databases | CC BY 4.0 | Separate from the code. Attribution string lives in `voices/*/PROVENANCE.md`. |
| Third-party MBROLA databases | Their own terms | Not redistributed. Free to copy when no charge is made, notice must travel, no sale, MBROLA use only. |
| `tools/mbrolator-src` | AGPL notices, no top-level metadata upstream | Offline tool, not in the add-on. Publishing the repo distributes it, so its sources and notices ship together. |
| `tools/ms_sam` | MIT reconstruction, Microsoft data excluded | Side experiment, not part of the add-on. |

Consequences worth remembering:

- The add-on may be given away but not sold as a bundle while it contains only
  GPL and AGPL code and our own CC BY data; that is now true, so the old
  "no-sale" constraint from stock databases is gone.
- Do not label the whole tree GPL-2.0-only. `LICENSES/GPL-2.0.txt`,
  `GPL-3.0.txt`, `AGPL-3.0.txt` and `VOICE-DATA.md` describe the split, and
  `NOTICE.md` lists each component, version and source.
- The GPL and AGPL require source for shipped binaries: both native binaries are
  rebuilt by `build.ps1` from this repository, and eSpeak NG and MBROLA are
  unmodified upstream code.
- A store submission needs `https://` URLs in the manifest and a name that does
  not collide with the upstream add-on. The manifest currently keeps the
  upstream name `espeakWitMbrola` so existing configurations keep working.

## The NVDA add-on

### Architecture

- NVDA's 64-bit process runs the Python synth driver and owns
  `nvwave.WavePlayer`.
- `espeakMbrolaHost.exe` is a 32-bit process. It loads the 32-bit eSpeak NG
  `espeak.dll` and `mbrola.dll`, initialises eSpeak with `espeak-ng-data`, and
  streams PCM and events to NVDA over framed standard input/output messages.
- `mbrola_stream.dll` is an optional 32-bit build of the project's MBROLA
  sources with the `LibOneChannel` API. The host feeds it the adjusted `.pho`
  text in memory and reads 20 ms blocks, so audio starts within a few
  milliseconds. It is a separate module from eSpeak's `mbrola.dll` and shares no
  state with it.
- Audio packets carry the sample rate and marker offsets. Index marks are
  attached to the audio block that precedes them, so a mark is announced when
  its audio has played, not when it was generated.
- `espeakwithmbrola.py` is a copy of NVDA's eSpeak driver with a voice filter.
  `_espeak_mb.py` is the bridge module; `getMbrolaVoiceList()` returns only
  `mb-*` voices whose database and phtrans file exist, so missing databases are
  never offered.

### Engine and voice descriptors

- Installed engine: `addon/synthDrivers/espeak.dll` is the 32-bit eSpeak NG
  `libespeak-ng.dll` (the official 1.51 X86 MSI; the DLL reports `1.52-dev`).
  Data is `addon/synthDrivers/espeak-ng-data`.
- Custom voices are `mb-usd16` and `mb-usd` in `espeak-ng-data/voices/mb/`,
  displayed as `us-mbrola-usd16` and `us-mbrola-usd`. The old identifiers
  `usd16`/`usd` are aliased in the driver so pre-3.0 configurations keep working.
- eSpeak 1.48 and eSpeak NG both assume 16 kHz unless the descriptor's `mbrola`
  line has a third field. `mb-usd` therefore reads
  `mbrola usd usd_phtrans 22050`, and `mb-usd16` reads
  `mbrola usd16 usd_phtrans 16000`. Stock 22.05 kHz voices use the same field.
- The driver filters its voice list with `_espeak_mb.getMbrolaVoiceList()`. On a
  full install that yields the standard MBROLA voices plus the custom ones; on
  this repository's build it yields only `mb-usd16` and `mb-usd` until the user
  adds databases.
- The host ignores `espeak_SetParameter` errors. eSpeak NG returns `EINVAL` for
  parameters 5 (punctuation), 6 (capitals) and 8 (options); treating those as
  fatal made synthesis fail with code -1.
- NVDA's own `synthDrivers/espeak.dll` is eSpeak NG 1.52.0 built **without**
  MBROLA support: no `mb-*` voices, `mb-en1` returns `EE_NOT_FOUND`, and MBROLA
  `.pho` output is empty. It cannot drive MBROLA; the separate 32-bit
  MBROLA-capable engine is required.
- SSML pitch is ignored by the MBROLA path in both eSpeak 1.48 and NG. Global
  pitch works. Capital pitch is intentionally not pursued; NVDA's
  beep-for-capitals covers it.
- At the same rate setting, NG is about 6-8% faster than the classic engine. The
  rate slider compensates.

### Rate timing

The host generates `.pho` at 175 wpm, rewrites durations to the requested target
with 25 ms vowel, 15 ms consonant and 8 ms pause floors, then synthesises the
adjusted stream. The driver maps slider 40 to 250 wpm and 65 to 400; rate boost
is 1.5x. With `mbrola.exe` the host output is bit-identical to the timing model
before the output gain. `mbrola.exe` is installed beside `mbrola.dll`.

### Loudness

Volume 100 applies 2.4x gain through a stateless soft-knee limiter (knee 0.70,
ceiling 0.97). No database rebuild is involved: units are stored at a healthy
level, and running speech peaks about 6 dB below full scale before gain, so the
output stage is the right place to raise loudness.

Reference sentence, 16 kHz: raw MBROLA output is -23.6 dBFS RMS and -6.2 dBFS
peak; at 1.6x it is -19.5 dBFS; at 2.4x it is -16.0 dBFS with a -0.4 dBFS peak
and zero clipped samples. Stock eSpeak NG `en-us` measures -20.3 dBFS RMS and
-2.0 dBFS peak on the same sentence, so the voice sits about 4 dB above it. The
22.05 kHz voice lands at -17.0 dBFS / -1.4 dBFS. The limiter runs after the gain
in both the streaming and `mbrola.exe` paths, touches only the loudest fraction
of a percent of samples, and never changes sample counts, so index marks stay
aligned. `ESPEAK_MB_GAIN` overrides the gain at volume 100 without a rebuild
(1.6 restores the old level; 2.8 or 3.0 are available for a louder test).

### Index marks, interruption and callbacks

The failure that motivated this design: `nvwave.WavePlayer.feed()` blocks while
the 400 ms device buffer is full, and `player.stop()` only discards what is
queued at that moment. A reader thread that keeps feeding the rest of an
interrupted packet lets up to a second of the old utterance resume.

Fixes, all in `_espeak_mb.py` and the host:

- One `_Feed` per utterance (`generation`, `aborted`, `pending`, `complete`,
  `doneNotified`). `stop()` aborts every feed, then stops the player.
  `_feedSegment()` checks the feed *after* `feed()` returns; if the utterance
  was aborted while the call was blocked, it stops the player again so the audio
  queued after the stop is discarded.
- The driver passes the ordered index list of each utterance to
  `_espeak_mb.speak()`. Marks eSpeak swallows are announced just before the next
  reported mark, or at the end of the utterance, so a lost mark can neither
  stall NVDA's speech queue nor reorder callbacks.
- `speak()` does not abort the previous utterance: NVDA decides interruptions by
  calling `cancel()` first. Queued speech plays back to back.
- Audio packets are 20 ms and marks are attached to the block of audio in front
  of them. Zero-length marks are carried to the next packet instead of firing
  early.
- `synthDoneSpeaking` fires when the last chunk has actually finished playing
  (`pending` reaches zero and the host has sent `DONE`), not when the host
  finishes sending.
- NVDA's WASAPI `WavePlayer` dispatches chunk-completion callbacks only from
  `feed()` or `sync()`/`idle()`. `_notifyDone` therefore calls `player.idle()` on
  the host's `DONE`, then fires any remaining expected indexes in order and
  notifies done-speaking. Without that flush the end-of-utterance index never
  arrived and NVDA never queued the next utterance.
- The host streams in 20 ms packets, so even an uncooperative interrupt leaves at
  most one block of old audio.

Measured offline with a simulated `WavePlayer`: normal marks fire at
0.516/1.016/1.704 s against audio offsets 0.51/1.00/1.70 s; done-speaking at the
end of the audio; an interrupt followed by `speak()` leaves 0 bytes of the old
utterance to feed; the new utterance's PCM starts at its first sample.

eSpeak NG drops a `<mark>` when it sits on a sentence boundary: `Hello. <mark/>World`
loses the mark, `Hello. <mark/>world` keeps it, and a mark at the very end is
dropped when the text ends with punctuation. Marks between words inside a clause
are reliable. That is an eSpeak defect, not a host bug. A zero-width space
before the mark fixes it but changes the audio in some texts, so it is not used.
The host used to drop a mark that landed exactly on the last sample, which also
lost the end-of-utterance index; that is fixed.

### Low latency streaming engine

`mbrola_stream.dll` is built from `tools/mbrola-src` (the same source as
`mbrola.exe`) with the `LibOneChannel` DLL API. The cached engine is closed and
re-opened per utterance because a reused instance carried state between
utterances (different audio and lengths on the second and later utterances).

Measurements (16 kHz voice, offline):

| text | first audio | whole utterance sent | exe path first audio |
| --- | --- | --- | --- |
| `check box` | 1.9-3.4 ms | 2.4 ms | 34 ms |
| one sentence | 2.8 ms | 5.0 ms | 36 ms |
| 14 s of text | 6.3 ms | 15.2 ms | 42 ms |

Audio comparison with the accepted `mbrola.exe` path on the same `.pho`:
durations match within 2 ms for most texts and markers land on identical byte
offsets. Short pauses are the one difference: the library build renders a pause
shorter than ~15 ms as 15 ms, while `mbrola.exe` renders it as 7.5 ms, so
utterances with many sub-15 ms pauses drift by up to about 1% and the waveforms
are pitch-period shifted in places. Set `ESPEAK_MB_MBROLA_EXE=1` to use
`mbrola.exe` instead; that path is bit-identical to the accepted build.

Live playback at rates 40 and 65 was verified by recording NVDA output: no
repeated sample blocks, no energy above the 22.05 kHz Nyquist, and the live
durations match the `.pho` timing model at 250/400 wpm. A 50 ms feed-coalescing
experiment did not remove the observed buzz and made index timing early, so it
was reverted. The add-on now passes listening at fast rates; no further
playback work is outstanding.

Proof details and listening samples are in
`output/pitch_test/NG_151_PROOF.md`.

### Packaging and build

`build.ps1` assembles the artifact:

1. copy `addon/` into `build/stage`;
2. copy eSpeak NG data from the vendor folder, excluding `mbrola/*`;
3. copy `espeak.dll`, `mbrola.dll` and `mbrola.exe` from the vendor folder;
4. compile `espeakMbrolaHost.exe` and `mbrola_stream.dll` (falls back to the
   vendor's prebuilt copies with a warning if `g++`/`gcc` are missing);
5. copy each `voices/<id>/` package into `espeak-ng-data/mbrola`,
   `mbrola_ph`, `voices/mb` and `doc/voices/<id>`;
6. verify required files and zip `dist/<name>-<version>.nvda-addon`.

Vendor source defaults to `$env:ESPEAK_MB_VENDOR`, then to the installed add-on.
Neither engine binaries nor eSpeak data are committed, and the build script never
downloads anything.

`installTasks.py` keeps user-added voices across updates: on install it copies
files from the installed add-on's `mbrola`, `mbrola_ph` and `voices/mb` folders
into the pending install, and from the legacy `espeak-data` equivalents, skipping
any file the new version already ships. That replaces the upstream 2.0 behaviour
of moving the whole folder, which would have deleted the newly shipped default
voice whenever the user had an older folder.

## Source recordings and alignment

- `Voice_audio/` contains the original, unmodified long WAV recordings (44.1 kHz
  stereo). Treat these as the authoritative audio source; never replace them
  with derived clips.
- `voice_file_ranges.txt` maps each original WAV to the script lines it contains.
- `output/segments/segments.csv` maps each per-line clip to its original WAV and
  records `source_start_s` / `source_end_s`. Line clips are 44.1 kHz mono.
- Phone-level TextGrids in `output/mfa_aligned/` use time coordinates relative to
  their per-line clip. To locate a phone in the original WAV, add the manifest's
  `source_start_s` to the TextGrid time.
- `output/unit_grid/unit_grid.csv` provides diphone labels, phone boundaries,
  source line identifiers and diagnostics, also relative to the per-line
  segment. Its `sample_rate` column is analyzer metadata, not the database rate.
- **Transcript/alignment caveat:** transcripts include a carrier `Say {target}`,
  but the recordings contain only the target phones. MFA places a false `say`
  word and `S EY` phones in the first ~80 ms of each line; that interval is
  silence. Target phone times are usable, but boundary units must be cut from
  the target word edges, not the `say` interval.
- **Target-only re-alignment (2026-10-26):** the carrier was never spoken, so
  `say` alignment was the main source of flagged units. The resegment tool now
  has `--no-carrier`, which writes token-only transcripts; a full re-run is the
  active `output/` corpus.
- **Line-range bug (2026-10-27):** `voice_file_ranges.txt` had the
  `DM550891`/`DM550893` split at 641/642, but `DM550893` starts at `a_th`
  (line 622). The split is now `DM550891` 267-621 and `DM550893` 622-662; the
  total is unchanged at 396 lines. That single error caused the silent
  `aa_`/`0_` block and the apparent 28 bad lines. With the corrected split and
  target-only alignment, 1,589 of 1,620 lines audit OK, 31 are edge-trim
  artifacts, and none are missing or misaligned. No re-recording is needed for
  that block. This is the reason the builder needs a line-map validator: every
  prompt must map to exactly one source range, once.

## Corpus state and coverage

- 1,666 script lines: 1,620 original plus 46 coda prompts from `DM550918.WAV`,
  lines 1621-1666.
- 2,333 units extracted, 0 failures.
- 1,073 unique diphone labels: 529 CV, 470 VC, 257 CCV, 58 VCC-derived.
- The coda batch added all 46 measured missing CC labels (plus `A:_r`), so the
  eSpeak top-10k coverage report shows 0 missing used coda clusters.
- Audit: 1,623 lines OK, 43 edge-trim artifacts, no missing or misaligned audio.
- Coverage of the recorded inventory: CV 529/529, VC 470/483 (13 vowel+`r` pairs
  intentionally omitted), onsets 27/27, VV 0, coda clusters 3/70. Of the 2,209
  possible pairs for the 47-phone set, 1,073 were recorded. Word-edge `_X`/`X_`
  units are not in the grid but are derivable for every legal phone.

## Full database

- Builder: `tools/build_mbrola_db.py`; report in
  `output/mbrola_db/BUILD_REPORT.md`.
- 2,303 units: 1,073 recorded (one automatic take selection per label, from
  2,333 takes), 94 word-edge units from real room tone, and 1,136 cross-spliced
  fills that complete the 47x47 pair matrix.
- MBROLA aborts an utterance on a missing pair (verified: `Fatal error: Unknown
  recovery for _-k segment`, exit -63) and eSpeak does not enable
  silence-substitution, so a partial database cannot be evaluated in NVDA. Fills
  splice the recorded second half of the left phone to the first half of the
  right phone using the articulatorily nearest donor; they are tracked in
  `manifest.json` and `fallback_pairs.csv`.
- `database_build` volume ratio is 0.8: at 1.0 synthesis clipped isolated
  samples (peak 32768); at 0.8 the worst measured output peaks at 32323 with zero
  clipped samples.
- Validation: all 2,303 diphones in eight `coverage_*.pho` chunks, 12 word tests,
  30 s of stress text and a 48,874-character technical text (54 minutes of
  audio), all through the installed eSpeak voice, no errors.
- Take selection uses SNR, MFA log-likelihood, edge-trim verdict, duration and
  level; it is not listening-based.
- `tools/make_espeak_voice.py` writes the phone table and descriptor.
- The phtrans record is six 4-byte fields: source, next-phoneme condition, first
  output, second output, split percent, control bits. Names are packed as up to
  four ASCII bytes. An earlier eight-byte-field writer put the second output into
  the split field, so only the first phone ever played; the layout was corrected
  on 2026-10-27 after comparing with `en1_phtrans`.
- The phone table has 53 sources and 65 records. Critical entries: `l/2` -> `l`
  (eSpeak's name for coda /l/), `@L` -> `@` + `l` (syllabic l, 50/50), `aI3` ->
  `aI` + `r` (70/30, "fire"), `aU@` -> `aU` + `r` (70/30, "flour"), `i@3` ->
  `i@` + `r` (70/30, "near"). Conditional records drop the added r when eSpeak
  already appends `r-`/`r` before a following vowel. "flower"/"player"/"employer"
  are synthesized as `aU`/`eI`/`OI` + `3` (the rhotic NURSE vowel), so they
  already had an r-coloured tail. Unmapped names are replaced by silence with the
  phone's duration.
- `tools/espeak_phoneme_dump.c` prints the mnemonics the installed DLL produces;
  build with `gcc -m32`. It works with both the classic 1.48 DLL and NG. The
  mnemonic display can differ from the name eSpeak sends to MBROLA (`l` versus
  `l/2`).
- Size is set by stored unit length, not unit count. 2,304 entries are the
  minimum for 47 phones (47x47 + 94 word edges + silence). The accepted build
  stores 487 s (median 210 ms per unit); en1 stores 209 s at 2,116 units (about
  99 ms). `build_mbrola_db.py --max-half-ms N` keeps at most N ms on each side
  of the junction. A 60 ms trial cut the database from 15.6 MB to 9.3 MB but made
  speech noticeably harder to understand, so the full-length build is kept.
- The 22.05 kHz build is 21.6 MB for the same 487 s: 22,050/16,000 more samples,
  nothing else.
- MBROLA supports replacement entries that point at another diphone's samples,
  so a pair can exist at no sample cost. Using that for the 1,136 spliced fills
  would save about 7 MB, but it reuses the donor's audio for both halves and
  loses the correct left phone, so it is not used.
- `data/phonemes.json` gained `aa`->`a`, `O:`->`O`, `I#`->`I` and `@/`->`@`;
  eSpeak en-us emits those allophones.
- Real-text coverage: the top-10k English words use 293 of the 1,136 gap pairs.
  `output/mbrola_db/gap_pairs_used.csv` lists them with examples;
  `gap_recording_prompts.txt` is a 244-word pickup list covering all 293, and
  `gap_prompts_map.csv` maps prompts to pairs. 843 gap pairs never occur in that
  vocabulary.

## MBROLA conversion requirements

- MBROLATOR `.seg` records need source audio, phone pair, first/transition/last
  samples. Input audio is mono signed 16-bit little-endian.
- `anaf0` and `anambe` are called with the sample range and a parameter file;
  `resynth` writes `.s16` and `.pm`; `database_build` packs the database.
- For 22.05 kHz, analyze 22.05 kHz audio with the 16 kHz constants, then pass the
  real rate to `database_build`. Do not change `Freq=16000` in the parameter
  files.
- `database_build <db> res/ res/ 2.069 <rate> <period> 1 [volume]` works in a
  build directory containing `<db>.seg`, `.f0`, `.mbe` and `.syn`. The period is
  120 at 16 kHz and 180 at 22.05 kHz for this speaker.
- A full voice also needs an eSpeak voice descriptor and a matching
  eSpeak-to-MBROLA phone translation table.

## MBROLA NG frontend

`mbrolaNG` is a separate NVDA add-on with its own letter-to-sound, duration and
pitch engine (`MBROLA_NG.dll` + `mbrola_ng_synth.exe`), language packs
(`languages/en.dat`, `hr.dat`) and voice catalog. It does not use eSpeak.

Its voice list is not hard-locked. A voice is a folder at
`%APPDATA%\nvda\mbrola_ng\voices\<id>\` containing the database named `<id>` and
an `<id>.voice` file with `language`, `base_pitch` and `phoneme_map`.
`tools/install_mbrola_ng_voice.py` writes both and updates `installed.json`.

- Installed custom voices: `usd16` (16 kHz) and `usd` (22.05 kHz), base pitch
  120.
- The English frontend uses ARPAbet-like names; the map is
  `A=A: AI=aI E=E EI=eI O=O i=i: u=u: r==3: k_h=k p_h=p t_h=t @U=oU {=a`.
  Unlisted names pass through unchanged. Without the map, most vowels and
  diphthongs are missing (1.5 s of speech instead of 4.1 s on the test
  sentence).
- The engine reads the database header, so the 22.05 kHz voice renders at
  22050 Hz with the same pitch as the 16 kHz one.
- Its prosody is its own; samples are in
  `output/mbrola_db/tests/mbrola_ng_*.wav`.
- It only ships English and Croatian language data, so new languages go through
  eSpeak unless MBROLA NG language packs can be produced.

The `.voice` files in `voices/` are a superset of what MBROLA NG needs: they add
`sample_rate`, `descriptor`, `phtrans`, `licence` and `provenance` so one package
can serve both engines and carry its own notices.

## Built offline toolchain

- `tools/mbrolator-src/AnaMBE/Obj/anaf0.exe`
- `tools/mbrolator-src/AnaMBE/Obj/anambe.exe`
- `tools/mbrolator-src/Resynthesis/resynth.exe`
- `tools/mbrolator-src/Resynthesis/database_build.exe`
- `tools/mbrola-src/Bin/mbrola.exe`

Built with w64devkit GCC 15.2. Local source patches/requirements:
`Resynthesis/Src/wincompat.c` supplies `drand48`; `database_build.c` guards the
Unix `date` and `/bin/rm` calls on Windows; AnaMBE must be compiled with
`-DLITTLE_ENDIAN`; `resynth` must open its `.s16`/`.pm` outputs with `"wb"`; and
`resynth` must be compiled with `-O0` because `-O2` miscompiles its harmonic
matching into NaN amplitudes and silent gaps. The C23 build also needs the
`drand48` prototype in `Src/wincompat.h`; build with
`gcc -O0 -fno-strict-aliasing -include Src/wincompat.h -o resynth
Src/resynth.c Src/wincompat.c -lm`. The previous optimized binary is kept as
`resynth_old_slow.exe` only as a reference; it was pathologically slow
(~7-21 s per unit versus ~0.03 s for the O0 build). If the sources are
re-cloned, these patches must be reapplied.

`mbrola_stream.dll` needs one source patch: `close_InputFifo` is no longer
`static` in `Parser/input_fifo.c` and is declared in `Parser/input_fifo.h`, plus
`readline_InputFifo` returns `long`. `build.ps1` runs the full command; it is
also documented in `addon/README.md`.

`mbrola.exe` does not support the documented `-d` option in this build; use
synthesis itself, or `-i`, to validate a database.

## Tiny database proof of concept

- Builder: `tools/build_mbrola_poc.py`.
- Outputs: `output/mbrola_poc/poc16/poc16`,
  `output/mbrola_poc/poc22050/poc22050`, and `wav/*.wav` for `see`, `tap`,
  `pat` and `all`. Build logs and `synth_results.json` sit beside them.
- Units: `_s`, `s_i:`, `i:_`, `_t`, `t_a`, `a_p`, `p_`, `_p`, `p_a`, `a_t`,
  `t_`. Silence-adjacent units are derived from room tone.
- `.seg` spans use midpoint-to-midpoint phone positions with the transition at
  the phone boundary. Audio context extends 0.12 s beyond the span.
- Both builds keep `Freq=16000` in `.f0`/`.mbe`/`.syn` and pass the real rate
  only to `database_build`. Per-rate MBE settings: 16 kHz uses
  `FrameLength=360`, `FrameShift=120`, period 120; 22.05 kHz uses
  `FrameLength=540`, `FrameShift=180`, period 180.
- The builder pads each `.s16` to a whole number of the rate's period, applies a
  fixed 3.5 gain to every unit, and calls `database_build` with a 1.0 volume
  ratio.
- The 22.05 kHz POC build takes about 2.4 s total (resynth ~0.3 s). Earlier
  artifacts are preserved in `output/mbrola_poc/poc22050_before_o0/` and
  `wav_before_o0/` for comparison.

## Inventory and active project files

- `data/phonemes.json` defines eSpeak General American phones and safe aligner
  token names. `onset_clusters.json` and `coda_clusters.json` define cluster
  coverage.
- `tools/generate_recording_script.py` regenerates prompts and their transcript,
  dictionary, diphone map and metadata. VCC prompts require `--codas`; they
  default to one prompt per cluster (carrier vowel `--coda-vowel`, default `A:`)
  because only the C1+C2 diphone is new. `--coda-all-vowels` restores the old
  per-vowel Cartesian behaviour. VV prompts are skipped when phonotactic cleanup
  is on.
- `tools/resegment_from_whole_alignment.py`, `build_mfa_dictionary.py`,
  `make_mfa_corpus.py` and `build_unit_grid.py` support segmentation and
  alignment for existing or future recordings.
- `tools/analyze_unit_grid.py` and `validate_segments.py` report alignment and
  recording outliers.
- `tools/audit_units.py` checks every unit against the original WAV (word-span
  energy, displaced speech, clipping, edge truncation) and writes
  `line_audit.csv`, `unit_audit.csv`, and pickup/repair lists.
- `tools/build_mbrola_poc.py` is the small end-to-end POC pipeline.
- `tools/build_mbrola_db.py` builds the full database: take selection, word-edge
  units, cross-spliced fills, analysis, packing and validation. It writes
  `take_selection.csv`, `unit_stats.csv`, `fallback_pairs.csv`,
  `build_summary.json` and `validation.json`.
- `tools/make_espeak_voice.py` writes `usd_phtrans` and the voice descriptors.
- `tools/install_mbrola_ng_voice.py` installs a database as a MBROLA NG voice.
- `tools/generate_rerecord_script.py` turns old review verdicts into pickup
  prompts.
- `docs/project_layout.md` classifies source, derived and cache files;
  `docs/voice_builder_plan.md` sketches the guided builder and language packs.
- `tools/mbrolator-src/` and `tools/mbrola-src/` are locally cloned and built
  sources; treat their build outputs as project tools.

`tools/mfa-env` is a junction to `C:\diphone-mfa-env`, outside this project
folder. Do not treat it as a normal project directory. `tools/micromamba.exe`
is the package manager. `tools/mfa-packages.txt` records the 196 packages in the
current environment, which can be recreated with
`tools\micromamba.exe create -p C:\diphone-mfa-env -c conda-forge --file
tools\mfa-packages.txt`. The `raw`, `ana`, `res`, `conv` and `SORTIE.*` build
intermediates were removed; the build recreates them from `output/unit_grid/`,
`output/segments/` and `Voice_audio/` in about two minutes per rate.

## Existing data to preserve

Keep `Voice_audio/`, `voice_file_ranges.txt`, the generated script/map/dictionary
files in `output/`, `output/segments/`, `output/mfa_aligned/` and
`output/unit_grid/`. These are the source and alignment basis for the converter.
They are kept on this machine only: the repository publishes the databases
(`voices/`), the audit data (`docs/database/`) and the reference build
(`fixtures/poc/`), and nothing else from these directories.
Keep `data/`, the finished databases and manifests
(`output/mbrola_db/usd16/usd16`, `output/mbrola_db/usd/usd`, `manifest.json`,
`BUILD_REPORT.md`), the packaged copies in `voices/`, and the add-on source in
`addon/`. The `raw`, `ana`, `res` and `SORTIE.*` intermediates are regenerable
(see `docs/project_layout.md`). Keep `output/mbrola_poc/` as the
proof-of-concept record. The saved `output/test_psola_see.wav` and
`output/test_world_see.wav` are comparison references;
`output/test_mbrola_bridge_en1.wav` is a known-runtime reference, not a sample of
the custom voice.

The partial `output/unit_review/review_progress.json` reflects an abandoned PSOLA
listening review. It may help identify recording lines to revisit, but it is not
an MBROLA quality assessment. `tools/generate_rerecord_script.py` can turn those
old `rerecord` verdicts into a pickup prompt list.

## Next work

1. **Release maintenance.** The repository is published as `DiphoneForge` with
   release `v3.0`. Future changes follow `docs/publishing.md`: bump the version
   in `addon/manifest.ini`, rebuild, verify with `tools/compare-install.ps1` and
   the smoke tests, then tag and release.
2. **Guided voice builder.** Turn the stage scripts into a `project.json`-driven
   pipeline: plan, import, align, grid, audit, build, package. Each stage needs
   to be idempotent, resumable, and free of English and path assumptions. Two
   acceptance tests: `fixtures/poc/` rebuilds anywhere and is the public
   regression check; rebuilding `usd16` locally and comparing the unit list and
   sample data with the accepted database is the maintainer's gate, since the
   corpus is private.
3. **Line-map validator.** Before anything else in the builder: assert that every
   prompt maps to exactly one source range, once, with no gaps. The
   `DM550891`/`DM550893` split cost a whole block and proved this is the most
   likely import mistake.
4. **Recording and review UX.** Prompt playback, per-file import, resume across
   sessions, automatic SNR and clipping flags, and a listening queue that turns
   flagged units into a pickup prompt list.
5. **Second language.** One complete language pack end to end, probably
   Croatian (MBROLA NG already has `hr.dat`) or Spanish (harder, tests clusters
   and carriers).
6. **Replace the audible cross-spliced fills.** `gap_recording_prompts.txt` is a
   244-word pickup batch covering the 293 gap pairs the top-10k vocabulary uses;
   `gap_prompts_map.csv` says which pairs each word provides.
7. Optional: context-dependent diphone variants (more than one take per label)
   once the single-take database is accepted.
8. Optional: eSpeak NG 1.52 32-bit built from source with MBROLA support, if
   exact parity with NVDA's bundled 1.52.0 is wanted. The current 1.51 X86 build
   reports `1.52-dev` and passes the custom-voice tests.
9. Optional: compensate for eSpeak's slightly shorter consonant durations at
   22.05 kHz (about 5-8% faster overall than the 16 kHz build); stock 22.05 kHz
   voices show the same tendency.

Capital pitch is not pursued; beep-for-capitals covers it.

## Closed side experiments

These are finished or parked. They are not part of the add-on and should not
consume time unless something concrete depends on them.

**Pitch model (2026-10).** `tools/pitch_model` and `output/pitch_test` explored
F0 extraction, contour rewriting and PSOLA-style manipulation. The result was
test-only: the accepted add-on uses eSpeak NG's own pitch, and no pitch model
code ships. Keep the directories as reference material and for the proof
document, not as a product path.

**Microsoft Sam-format voice (2026-10-28/29).** The recordings were also used to
build a Sam-format `.spd` (`output/ms_sam/voice/usd22_sent.spd`) through
`tools/ms_sam`, a C99 reconstruction of the XP SAPI engine plus a re-voicing
pipeline. It works as a synthesiser but is a side experiment: Sam is a
single-phone LPC vocoder, so diphone transitions are unused, and there is no SAPI
or NVDA driver. Sentence-corpus coverage reached 3,081 of 3,365 units (91.6%).
Nothing here should take priority over MBROLA. Keep it contained in
`tools/ms_sam/` and `output/ms_sam/`.

## Session rules

- Keep original recordings unchanged; work from copies or read them as sources.
- Preserve the manifest-to-TextGrid time mapping when correcting alignments.
- Update this file and `roadmap.md` when the pipeline or project state changes.
- Keep `addon/` free of builder code, corpora and generated data.
- Never commit `Voice_audio/` or `output/`; both are excluded by `.gitignore`.
  Publish derived material through `voices/`, `docs/database/` or
  `fixtures/poc/` instead.
- Never commit or distribute third-party MBROLA databases.
- Do not commit or push unless explicitly requested.
- When adding a dependency or a vendored source, add it to `NOTICE.md` with its
  version, licence and source URL.
