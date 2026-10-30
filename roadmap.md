# MBROLA Voice Database Roadmap

## Status at 2026-10-30

The database roadmap below is complete, and the deliverable has moved on: the
NVDA add-on is stable, the repository is structured around it, and the next
phase is the reusable voice builder. Authoritative current state, licensing and
repository layout live in `AGENTS.md`; this file remains the build history and
the database-specific notes.

## Objective

Build a small US English MBROLA voice database from the project's human
recordings, load it through the existing eSpeak-with-MBROLA NVDA path, and
continue to a full inventory only if the small voice passes a listening check.

## Current status

### Complete

- eSpeak General American phoneme inventory and CV, VC, and two-consonant onset
  prompt generator are in `data/` and `tools/generate_recording_script.py`.
- The first voice has 18 original long WAV recordings covering 1,620 prompts;
  `voice_file_ranges.txt` maps WAV files to prompt-line ranges.
- Whole-file forced alignment and per-line phone alignment were completed.
  `output/segments/segments.csv` retains each line's source WAV and source-time
  offset; `output/mfa_aligned/` contains the line-relative phone TextGrids.
- `output/unit_grid/unit_grid.csv` records 2,241 CV/VC/CCV-derived units across
  1,026 unique diphone labels, with phone-boundary times and diagnostics.
- The project-local NVDA 2026 x64 bridge to the installed x86 eSpeak/MBROLA
  runtime was confirmed working by the user. Existing `mb-en1` smoke output is
  available as a bridge reference.
- The offline MBROLATOR toolchain builds natively with w64devkit GCC 15.2:
  `anaf0`, `anambe`, `resynth`, and `database_build` are in
  `tools/mbrolator-src/`, and `mbrola.exe` is in `tools/mbrola-src/Bin/`.
  No Cygwin or WSL installation is needed.
- `tools/build_mbrola_poc.py` converts phone boundaries into MBROLATOR `.seg`
  records, extracts context-preserving audio from the original recordings,
  runs analysis/resynthesis, and packs databases.
- Two tiny proof-of-concept databases exist:
  `output/mbrola_poc/poc16/poc16` (16 kHz) and
  `output/mbrola_poc/poc22050/poc22050` (22.05 kHz).
- Test audio for `see`, `tap`, `pat`, and a combined utterance is in
  `output/mbrola_poc/wav/` for both rates.
- After fixing the Windows/toolchain defects and the 22.05 kHz frame-period
  mismatch (below), both the 16 kHz and 22.05 kHz WAVs are speech-like, have
  the expected speaker pitch (~121-124 Hz), and have no long internal silence
  gaps. Both are awaiting a listening verdict.

### Debugging fixes

1. AnaMBE was never defining `LITTLE_ENDIAN`, so `SoundRAW` byte-swapped every
   input sample. Rebuilt with `-DLITTLE_ENDIAN`; analysis now matches the
   upstream MBROLATOR TEST reference.
2. `resynth` opened `.s16` and `.pm` in text mode. On Windows this inserts CR
   bytes into the waveform, which caused the original crackling. Both outputs
   now open with `"wb"`.
3. `resynth` at `-O2` produced NaN harmonic amplitudes for low-F0 frames,
   which the voiced/unvoiced decision treated as voiced and synthesized as
   silence. Rebuilt with `-O0 -fno-strict-aliasing`; the 16 kHz units now have
   correct unvoiced frames and no internal zero gaps.
4. The 22.05 kHz build used the 16 kHz MBE frame shift (120 samples) with
   22.05 kHz audio. The speaker's period at 22.05 kHz is about 180 samples, so
   the analysis frames were shorter than one period and voiced vowels
   collapsed. The 22.05 kHz build now uses `.mbe FrameLength=540`,
   `FrameShift=180`, and database period 180 while keeping `Freq=16000`.
5. The installed `resynth.exe` was pathologically slow (~7-21 s per unit).
   Rebuilding the same source with `-O0 -fno-strict-aliasing` and the
   `wincompat.h` prototype reduced it to ~0.03 s per unit. The old binary is
   kept as `resynth_old_slow.exe`. The 11-unit 22.05 kHz build now takes about
   2.4 s total.

### Coverage audit

- CV: 529/529 complete.
- VC: 470/483. The 13 absent pairs are vowel + `/r/` combinations represented
  by r-colored vowels.
- Onset clusters: 27/27 complete.
- VV transitions: 0 recorded.
- Coda clusters: 3/70, and those three are onset-cluster overlaps.
- Word-initial and word-final silence boundaries: not in the unit grid, but
  derivable from the silence around every isolated target word. All legal
  phones have donors with at least 0.85 s of context.
- 47/47 phones appear in the inventory.

## Tiny database details

The POC uses 11 diphones:

`_s`, `s_i:`, `i:_`, `_t`, `t_a`, `a_p`, `p_`, `_p`, `p_a`, `a_t`, `t_`.

- `s_i:`, `t_a`, `a_p`, `p_a`, and `a_t` come from selected aligned CV/VC
  units.
- `_s`, `i:_`, `_t`, `p_`, `_p`, and `t_` are derived from the room tone
  immediately before or after the target word.
- The weakest unit is `a_t`: its donor vowel is only 40 ms and is flagged
  `mostly_unvoiced`. Listen closely to `pat` when judging the POC.
- `.seg` spans use midpoint-to-midpoint phone positions, with the transition
  at the phone boundary. Analysis context extends 0.12 s beyond the span.
- Both databases keep `Freq=16000` in `.f0`/`.mbe`/`.syn`; the 22.05 kHz
  database passes 22050 only to `database_build`, which is the documented
  22 kHz path.
- Per-rate MBE frame settings: 16 kHz uses `FrameLength=360`,
  `FrameShift=120`, period 120; 22.05 kHz uses `FrameLength=540`,
  `FrameShift=180`, period 180. The 22.05 kHz values cover the speaker's
  ~122 Hz period at 22.05 kHz.
- Resynth outputs are padded to a whole number of the rate's period; every
  unit receives a fixed 3.5 gain, and `database_build` is called with a 1.0
  volume ratio. This keeps the output at a usable level without saturation.
- Build logs: `build16.log`, `build22050.log`, `build_fixed.log`, and
  `build22050_o0.log`. The first three describe earlier builds; the current
  22.05 kHz build is `build22050_o0.log`. Use `synth_results.json` for the
  current WAV measurements. The previous 22.05 kHz DB and WAVs are in
  `poc22050_before_o0/` and `wav_before_o0/` for comparison.

### Alignment caveat

The transcripts say `Say p a` (and similar), but the recordings contain only
the target phones. MFA places a false `say` word and `S EY` phones in the first
~80 ms of each line. Those intervals are silence, not speech. The target phone
times are usable; boundary units must be cut from the target word edges, not
from the `say` interval.

## Full database (2026-10-27)

The full database is built and installed; details are in
`output/mbrola_db/BUILD_REPORT.md`.

- `tools/build_mbrola_db.py` selects one take per recorded label, derives the
  94 word-edge units from room tone, cross-splices the 1,136 unrecorded pairs,
  runs the analysis chain, packs the database and validates it.
- 2,303 units in total; 16 kHz database 15.6 MB, 22.05 kHz database 21.6 MB.
- The build intermediates (`raw`, `ana`, `res`, `conv`, `SORTIE.*`), the old
  pilot and the micromamba package cache were removed on 2026-10-27. The final
  databases, manifests and validation reports remain; the build recreates the
  intermediates from the unit grid in about two minutes per rate. See
  `docs/project_layout.md`; `tools/mfa-packages.txt` records the MFA packages.
- Both builds work in NVDA. The 22.05 kHz descriptor must carry the optional
  rate field (`mbrola usd usd_phtrans 22050`); without it eSpeak 1.48 assumes
  16 kHz and plays the 22.05 kHz database 1.375x slow and low. See below.
- `database_build` volume ratio 0.8 removes the isolated clipping that 1.0
  produced.
- The voice descriptor and phtrans table are generated by
  `tools/make_espeak_voice.py`. The descriptor must be installed in the
  `voices` root (`espeak-data/voices/usd16`), not `voices/mb/`: this eSpeak
  build ignores newly added files in `voices/mb`.
- Validation: every diphone synthesized in `coverage_*.pho`, 12 word tests,
  30 s of stress text and a 48,874-character technical text (54 minutes), all
  with no errors and no clipped samples.
- Real-text gap: the top-10k words use 293 of the 1,136 cross-spliced pairs.
  `gap_recording_prompts.txt` is a 244-word pickup list covering them.

## Listening fixes (2026-10-27, second pass)

The first pass fixed coda /l/ (eSpeak sends `l/2`) but the two-phone mappings for
`@L`, `aI3` and `aU@` had no effect. `tools/make_espeak_voice.py` was writing
eight-byte name fields, while the engine's record is six four-byte fields:
source, next-phoneme condition, first output, second output, split percent,
control bits. The second output landed in the split-percent field, so only the
first phone was spoken (`@L` -> schwa, `aI3` -> `aI`, `aU@` -> `aU`).

The writer now emits the real layout and has conditional records. Verified
against the installed en1 table, which uses the same layout for
`aI@` -> aI + @ and `aU@` -> aU + @.

- `@L` -> `@` + `l` (50/50): "table", "channel", "available", "little".
- `aI3` -> `aI` + `r` (70/30): "fire", "require", "tired".
- `aU@` -> `aU` + `r` (70/30): "flour", "hour", "sour".
- `i@3` -> `i@` + `r` (70/30): "near", "here".
- Before a following vowel eSpeak appends `r-` or `r` itself ("fire is" is
  `aI3 r- I z`). Conditional records drop the added r in that case, so it is
  not spoken twice.
- eSpeak's synthesis names can differ from the mnemonic dump: "flower",
  "player" and "employer" are synthesized as `aU`/`eI`/`OI` + `3` (the rhotic
  NURSE vowel), not as `aU3`/`eI3`/`OI3`. Those words already had an r-coloured
  tail and are unchanged. The `aU3`/`eI3`/`OI3` entries are kept as safety
  mappings for other dialects and displays.

Verification: `channel` and `little` now end with F2 near 600-950 Hz (dark /l/)
instead of 1480-2030 Hz; `fire` shows the expected F1 rise/F2 fall of a coda
/r/. The table is 65 records from 53 sources.

## Database size (2026-10-27)

The database is 15.6 MB because of stored unit length, not unit count. It
already contains the minimum entry set for the 47-phone inventory: 47x47 pairs
plus 94 word-edge units plus the silence diphone.

| Database | Units | Stored audio | Size |
| --- | --- | --- | --- |
| usd16 (accepted) | 2,304 | 487 s | 15.6 MB |
| en1 | 2,116 | 209 s | 6.7 MB |
| us1 | 2,025 | 225 s | 7.2 MB |

The accepted build stores a median 210 ms per unit; en1 stores about 99 ms.
The spans are midpoint-to-midpoint from sentence alignment and include the
outer steady state of each phone. MBROLA stretches each half to the duration it
is given, so the outer steady state is not needed.

`tools/build_mbrola_db.py --max-half-ms N` keeps at most N ms on each side of
the phone junction, preserving the transition, closure and burst. Simulated
sizes: 60 ms -> 8.9 MB, 80 ms -> 11.0 MB, 100 ms -> 12.6 MB. A 60 ms trial
build (9.29 MB) was made and **rejected on a listening check**: speech became
noticeably harder to understand, so the full-length build is kept. The option
remains for experiments.

MBROLA's format also supports replacement entries that point at another
diphone's samples, so a pair can exist at no sample cost. That could remove the
1,136 spliced fills from the sample data, but each duplicate would reuse a
donor's audio for both halves and lose the correct left phone, so it is not
used while the spliced fills are preferred.

## 22.05 kHz in eSpeak (2026-10-27)

eSpeak 1.48 does not read the sample rate from the database header. Its voice
parser takes an optional third field on the `mbrola` line and defaults to
16000 when it is absent:

```
mbrola usd usd_phtrans 22050
```

Without that field eSpeak generates 16 kHz audio while MBROLA emits 22.05 kHz
samples, so playback is 1.375x slow and low. The stock 22.05 kHz voices
(`mb-br2`, `mb-br3`, `mb-es2`) all include the field. Adding it makes eSpeak
set `samplerate`, emit its `SAMPLERATE` event with 22050, and the host report
and play 22050 Hz.

`tools/make_espeak_voice.py --rate 22050` writes the field. The 22.05 kHz voice
is installed as **`us-mbrola-usd`**. It measures 102.1 Hz against 101.3 Hz for
the 16 kHz voice on the same text, and no longer sounds slow or low.

The 22.05 kHz build runs about 5-8% faster overall than the 16 kHz build for the
same text. Stock 22.05 kHz voices show the same shorter-consonant tendency
(`es1` 5.02 s speech vs `es2` 4.85 s on the same Spanish sentence), so it comes
from eSpeak's consonant timing at the higher rate, not from the database.

## eSpeak NG migration (2026-10-27)

The add-on now uses eSpeak NG 1.51 X86 (the official 1.51 MSI; its DLL reports
`1.52-dev`) with the existing 32-bit host and MBROLA DLL. The custom voices are
`mb-usd16` and `mb-usd` in `espeak-ng-data/voices/mb`; the driver filters the
voice list to 15 usable MBROLA voices whose database and phtrans file exist.
The host ignores `EINVAL` from NG for parameters 5, 6, and 8. The classic engine
is preserved in `synthDrivers/classic-148-backup`.

NVDA's own eSpeak 1.52.0 is built **without MBROLA support**: no `mb-*` voices,
`mb-en1` returns `EE_NOT_FOUND`, and MBROLA `.pho` output is empty. It cannot
drive these voices; the separate 32-bit MBROLA-capable engine is required.

SSML pitch and embedded pitch commands are still ignored by the MBROLA backend;
only the global `espeakPITCH` parameter works. Capital pitch therefore needs
segmented synthesis with per-segment pitch. At the same rate setting, NG is
6-8% faster than the classic engine.

## Rate management and MBROLA limits (2026-10-27)

eSpeak's rate parameter maps to 80-449 wpm. The driver's rate boost multiplies
it by 3, but eSpeak clamps at about 449, so boost makes slider 0 equal 240 wpm
and the whole upper range the same 449 wpm output.

The generated `.pho` durations explain the choppiness and buzzing:

| eSpeak rate | vowel min/median | consonant min/median | total |
| --- | --- | --- | --- |
| 80 wpm | 56 / 138 ms | 114 / 118 ms | 5.39 s |
| 175 wpm | 18 / 46 ms | 39 / 65 ms | 2.48 s |
| 220 wpm | 12 / 29 ms | 24 / 54 ms | 1.91 s |
| 264 wpm | 7 / 18 ms | 15 / 47 ms | 1.54 s |
| 300 wpm | 4 / 11 ms | 10 / 43 ms | 1.31 s |
| 350 wpm | 2 / 4 ms | 4 / 37 ms | 1.03 s |
| 449 wpm | 1 / 4 ms | 4 / 14 ms | 0.49 s |

At 449 wpm every vowel is 1-6 ms while consonants keep a 14-35 ms floor, so
MBROLA loops/discontinues the short units. Rewriting the `.pho` with minimum
durations of 30 ms for vowels, 18 ms for consonants, and 10 ms for pauses
changes the 449 wpm output from 0.484 s to 0.806 s and removes the extreme
short vowels. Proof WAVs are in `output/pitch_test/rate_fix/`.

The controls available are:

- eSpeak parameters only: rate, pitch, range, word gap. No per-phone duration
  control, and the rate algorithm collapses vowels at high speeds.
- `.pho` post-processing: full control over every phone duration and pitch
  point before MBROLA. Needs host changes and marker remapping.
- Sonic time-stretch: synthesize at a safe eSpeak rate and speed up the PCM.
  Preserves the natural vowel/consonant balance and pitch; needs a 32-bit
  Sonic build or bridge-side integration.
- Safe-rate cap: remap the slider to about 220-240 wpm and reduce/disable
  boost. Stable, but limits top speed.

Diagnosis on 2026-10-27: host PCM at 449 wpm matched the offline MBROLA output
almost exactly, and bridge marker splitting preserved the PCM sample-for-sample.
A 50 ms feed-coalescing experiment did not remove the buzz and made index timing
early, so it was reverted. A 3-4 ms resonance is present in both live and
offline 22.05 kHz audio, so it is not introduced by the bridge. The 22.05 kHz
voice is the main suspect at high rates; the 16 kHz voice is the control.
eSpeak NG also dropped some `<mark>` elements after sentence boundaries in a
long test; check NVDA index tracking separately.

22.05 kHz period hypothesis (2026-10-27): the 22.05 kHz database uses period
180, chosen for the speaker's natural ~122 Hz. At pitch 55 the voice is around
135 Hz, so the matching period is closer to 163 samples. That ~10% mismatch can
produce a half-period/3-4 ms resonance. The 16 kHz database uses period 120,
which is closer at that pitch, and its measured short-lag resonance is weaker
(0.169 vs 0.204). Test `mb-usd16` live before rebuilding the 22.05 kHz database
with a shorter period.

Implemented 2026-10-27: the host now generates `.pho` at 175 wpm, rewrites
durations to the requested target with 25 ms vowel, 15 ms consonant and 8 ms
pause floors, and runs `mbrola.exe` on the adjusted stream. The driver maps
slider 40 to 250 wpm and 65 to 400; rate boost is 1.5x. The host output is
bit-identical to the timing model before the 1.6 volume gain. `mbrola.exe` is
installed beside `mbrola.dll`; if it is missing the host falls back to the old
eSpeak audio path. **Accepted on a listening check (2026-10-27):** rate scales
cleanly with boost on and off, vowels are natural, and stock MBROLA voices also
improve.

Live recording analysis (2026-10-27): the user's rate 40 and 65 recordings are
44.1 kHz stereo, identical channels, 4.226 s and 2.916 s total. They contain no
exact repeated sample blocks and no energy above the 22.05 kHz Nyquist, so the
bridge and resampling are clean. The live recording is louder and slightly
brighter than the offline files, especially at rate 65. A `.pho` timing model
at targets 250/400 with 25 ms vowel floors matches the live durations
(4.104 s vs 4.125 s; 2.708 s vs 2.754 s). Normalized model WAVs are in
`output/pitch_test/timing_model/`.

## MBROLA NG and the builder plan (2026-10-27)

`mbrolaNG` is a separate NVDA add-on with its own letter-to-sound, duration and
pitch engine. Its voice list is a folder scan, not a hard-coded catalog:

```
%APPDATA%\nvda\mbrola_ng\voices\<id>\<id>
%APPDATA%\nvda\mbrola_ng\voices\<id>\<id>.voice
```

`tools/install_mbrola_ng_voice.py` installs a database there with a phoneme map.
The custom database is installed as `usd16` and `usd`, base pitch 120, and the
add-on lists both. The English frontend uses ARPAbet-like names; the map is
`A=A: AI=aI E=E EI=eI O=O i=i: u=u: r==3: k_h=k p_h=p t_h=t @U=oU {=a`.
Without it most vowels are missing, which confirms the map is applied.

The engine reads the database header, so `usd` renders at 22050 Hz. Its prosody
is simpler than eSpeak's but not flat (F0 sd about 22 Hz on the test paragraph).
Samples: `output/mbrola_db/tests/mbrola_ng_{en1,usd16,usd22}.wav`.

The add-on only ships `en.dat` and `hr.dat`, so other languages would still go
through eSpeak unless MBROLA NG language packs can be produced.

`docs/project_layout.md` lists what is source, derived and safe to delete
(the `raw`/`ana`/`res` intermediates alone are about 600 MB).
`docs/voice_builder_plan.md` sketches the guided builder: a project folder,
per-language packs, a recording/range editor, MFA alignment, build and install,
with a wxPython wizard over the same CLI commands.

The NVDA driver now creates its WavePlayer on the first audio packet at the
voice's real rate instead of opening a 22.05 kHz player at startup. The first
utterance after switching to a 16 kHz voice could previously be cut during the
player swap.

## Index marks, interruption and low latency (2026-10-27/28)

Interrupting NVDA while an utterance was being spoken could resume a later part
of that utterance at an index-mark boundary and skip the start of the new text.
`nvwave.WavePlayer.feed()` blocks while the 400 ms device buffer is full, and
`stop()` only discards what is queued at that moment, so the reader thread kept
feeding the rest of the interrupted packet after the stop. A simulated player
reproduced it: 0.62 s of the interrupted utterance was queued after `stop()`.

`_espeak_mb.py` now tracks one feed per utterance, aborts it on `stop()`,
re-checks after every blocking `feed()` and stops the player again if the
utterance was cancelled, so no cancelled audio can start playing. Index marks
are attached to the audio block in front of them, so a mark is announced when
its audio has played; `synthDoneSpeaking` likewise waits until the last chunk
has finished playing instead of firing when the host finishes sending.

The host now streams 20 ms packets, synthesises through
`mbrola_stream.dll` (the project's MBROLA sources built as a one-channel DLL,
loaded as a separate module from eSpeak's `mbrola.dll`) and keeps the engine in
memory, so the first audio of an utterance arrives in about 2-6 ms instead of
35-50 ms. Short pauses below 15 ms are rendered as 15 ms by the library build
versus 7.5 ms by `mbrola.exe`; durations still agree within about 1%.
`ESPEAK_MB_MBROLA_EXE=1` forces the `mbrola.exe` path, which is bit-identical
to the previously accepted output.

Two eSpeak NG index findings: a `<mark>` on a sentence boundary is dropped
(`Hello. <mark/>World`), and the host used to drop a mark that landed exactly
on the last sample, which lost the end-of-utterance index. The latter is fixed;
the former is an eSpeak defect that no longer affects the final index when the
mark is reported at all.

The remaining cause of dropped queued speech and silent mark-only utterances
(2026-10-28): NVDA's WASAPI player dispatches chunk-completion callbacks only
from `feed()` or `sync()`/`idle()`; its playback thread never calls them. The
bridge relied on `onDone` for the last chunk, so the index attached to the end
of an utterance (and `synthDoneSpeaking`) never arrived. NVDA then kept that
utterance in `_indexesSpeaking` and never pushed the next one. Symptoms: say all
stopped after the first line, a window title was not followed by the focused
element, an utterance consisting only of an earcon (a speech-dictionary sound,
or beep-for-capitals on a lone capital) was silent, and trailing earcons after
speech were lost. `_notifyDone` now calls `player.idle()` when the host reports
`DONE`, exactly as NVDA's own eSpeak driver does, which flushes the final
callbacks, fires every remaining expected index in order and then notifies
`synthDoneSpeaking`. A WASAPI-like simulated player reproduced all four
symptoms before the change and passes after it; leading marks still fire early
and trailing marks at the end of the audio.

## Not yet done

- 1,136 cross-spliced pairs are synthetic; the 293 used by common English are
  listed for real pickup recording.
- Only one take per diphone; no context-dependent variant selection.
- Take selection is automatic, not listening-based.
- The 22.05 kHz build runs 5-8% faster than the 16 kHz build; this is eSpeak's
  consonant timing at the higher rate and stock 22.05 kHz voices show it too.
  eSpeak NG is another 6-8% faster than the classic engine at the same setting.
- Capital pitch is intentionally not pursued. The MBROLA backend ignores both
  SSML `<prosody pitch>` and eSpeak embedded pitch commands; beep-for-capitals
  is used instead.
- NVDA's internal eSpeak cannot drive MBROLA because NVDA builds it without
  MBROLA support. A separate 32-bit MBROLA-capable eSpeak NG DLL is required.

## Next work

0. **Accepted on listening (2026-10-27).** The eSpeak NG `.pho` timing
   engine, rate scaling with boost on and off, and the custom voice all sound
   correct. Stock MBROLA voices also improve.
1. **Record the pickup batch.** `output/mbrola_db/gap_recording_prompts.txt` and
   `gap_prompts_map.csv` cover the 293 missing pairs that the top-10k
   vocabulary uses. Importing them and rebuilding replaces most splices.
2. **Fix individual takes only where listening shows a problem.**
3. **Optional:** context-dependent diphone variants.
4. **Rate timing is accepted.** No further tuning is needed unless a specific
   voice misbehaves; the `.pho` path falls back to eSpeak audio if
   `mbrola.exe` is missing.
5. **Optional:** build eSpeak NG 1.52 32-bit from source with MBROLA support if
   exact parity with NVDA's bundled 1.52.0 is wanted. The current 1.51 X86
   build already reports `1.52-dev`.

## Loudness (2026-10-29)

The completed voice was reliable but felt quiet next to other synthesizers. A
database rebuild is not the fix: the stored units are already at a healthy
level (median unit peak is about a quarter of full scale, which is normal for a
single take) and quantization noise is irrelevant at 16-bit. What limits
loudness is the crest factor of the synthesized stream, not the stored samples.

Measured on the reference sentence through the host at rate 175:

| signal | RMS | peak |
| --- | --- | --- |
| raw MBROLA output (16 kHz) | -23.6 dBFS | -6.2 dBFS |
| old host gain 1.6x | -19.5 dBFS | -2.1 dBFS |
| new host gain 2.4x | -16.0 dBFS | -0.4 dBFS |
| stock eSpeak NG en-us | -20.3 dBFS | -2.0 dBFS |

The 22.05 kHz voice measures -17.0 dBFS RMS and -1.4 dBFS peak at 2.4x.
Normalizing individual units was rejected: it would flatten the natural
balance between vowels and quiet consonants, and per-unit level jumps would
land on diphone joins.

Implementation: `PHO_VOLUME_GAIN` is now 2.4, applied through a stateless
soft-knee limiter (linear to 0.70, `tanh` bend to a 0.97 ceiling) in both the
streaming and `mbrola.exe` paths. The limiter touches only the loudest fraction
of a percent of samples on running speech, adds no latency and changes no
sample counts, so markers and the 20 ms packet schedule are unaffected. The
fast path and the `mbrola.exe` path now measure within 0.02 dB of each other
(-16.04 vs -16.02 dBFS). `ESPEAK_MB_GAIN` overrides the gain at volume 100 for
ear testing.

## Microsoft Sam-format voice (2026-10-28)

**Side experiment only.** It answers "can these recordings drive that engine at all"; it is not a
product path and never replaces the MBROLA database, which is the real target and is more stable and
versatile. Keep it in `tools/ms_sam/` and `output/ms_sam/`.

A separate experiment, not part of the MBROLA path: the decompiled SAPI 5 XP
engine (`ms-sam-mike-mary-decomp`) will speak with a `.spd` trained from these
recordings. Sam's format is a 20th-order LPC vocoder with 3,365 context units,
not a diphone database, so only the phones are reused; the diphone transitions
are not.

- 22.05 kHz build `output/ms_sam/voice/usd22.spd` (plus a 16 kHz `usd16.spd`),
  built with `tools/ms_sam/`; samples and `BUILD_REPORT.md` beside it.
- 872 / 3,365 units (25.9%) get a context-matching recording, 2,461 reuse the
  same phone's typical take, 32 are silence. All 47 phonemes are present.
- The first build whispered on every voiced sound. The cause was the stored
  pulse phase: the analysis marks drift, so consecutive pulses disagreed.
  Locking each unit to one pulse (`--pulse phase0`) fixed that but sounded
  robotic (pulse correlation 0.91; real Sam 0.67-0.86). The default is now
  `--pulse smoothblend --passes 4 --blend 0.55`: smoothed phases pulled 55%
  toward the unit's loudest pulse, pulse correlation 0.85, paragraph
  autocorrelation 0.67 vs Sam's 0.63, harmonic energy 0.814 vs 0.748.
- Mike and Mary have identical tables/CART and differ only in durations and base
  pitch; coverage with their tree is 26.8% vs Sam's 25.9%, so there is no
  meaningful quality gain from switching template.
- A 759-sentence corpus (38 min, phone recordings) aligned with MFA raises
  context coverage to 3,081/3,365 units (91.6%) and fixes the slow timing
  (18.36 s vs Sam's 18.91 s). Voice `usd22_sent.spd`; see `samples/sent/`.
- Output is 12% slower than Sam with the word list; the sentence voice matches
  Sam's length. It cannot match the MBROLA voice's fidelity.
- Blocked for general use only by integration: no SAPI 5 COM engine and no NVDA
  synth driver exist; `sam.dll` exports a screen-reader API instead.
- `Sam.spd` is Microsoft data from Speech SDK 5.1; the built voices contain
  Microsoft's tables and stay local.

## Conversion inputs and safeguards

- Preserve `Voice_audio/`, `voice_file_ranges.txt`, `output/segments/segments.csv`,
  `output/segments/`, `output/mfa_aligned/`, `output/unit_grid/unit_grid.csv`,
  `output/diphone_map.json`, dictionaries, and `data/`.
- TextGrid phone times are relative to the corresponding line clip. Use the
  `source_start_s` from `segments.csv` to map those times back into the original
  recording. The line clips are alignment aids; the original WAVs provide the
  source audio and extra context.
- The original recordings are 44.1 kHz stereo; the per-line clips are 44.1 kHz
  mono. Resample only after extracting from the original.
- Do not use the unit grid's `sample_rate` column to index audio. It is an
  analyzer value, not the final database rate.
- Keep the recording guide for future sessions. Follow its prompt/transcript
  count discipline and retain untouched originals for every new recording.

## References

- MBROLA: <https://github.com/numediart/MBROLA>
- MBROLATOR: <https://github.com/numediart/MBROLATOR>
- eSpeak-NG MBROLA integration: <https://github.com/espeak-ng/espeak-ng/blob/master/docs/mbrola.md>
- Project database and toolchain notes: `docs/mbrola_direction.md`
- NVDA bridge implementation and validation: `docs/nvda_2026_mbrola_port.md`
- POC builder: `tools/build_mbrola_poc.py`
