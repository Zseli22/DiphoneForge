# A Microsoft Sam-format voice from this project's recordings

Status: **working prototype** (2026-10-28), **excitation fixed** (2026-10-29).  The decompiled SAPI 5
"Microsoft TTS engine" (`spttseng.dll`, the Windows XP Sam/Mike/Mary engine) is built from
[`KamiKitsune420/ms-sam-mike-mary-decomp`](https://github.com/KamiKitsune420/ms-sam-mike-mary-decomp)
and speaks with a `.spd` voice whose synthesis section was trained on this project's recordings.

The first build spoke but every voiced sound was breathy ("bronchitis"): the stored pulse of one pitch
period carried the phase of a drifting analysis window, so consecutive pulses disagreed and the engine's
voiced excitation turned into noise.  Keeping one real pulse shape per unit (its loudest frame) while
keeping each frame's magnitude envelope removed the whisper.  See *Pulse phase* below.

The signal chain is the opposite of MBROLA's.  Sam is a **single-phone LPC vocoder**: every phone is a
short list of pitch-synchronous frames (period length, 20 LSFs as 6 split-VQ bytes, gain, and for voiced
frames a residual pulse spectrum as 11 VQ bytes).  A CART per phone picks one of 3,365 units from the
phone, its left and right neighbours and its position in the word.  So the recordings are not used as
diphones: each phone is cut out, analysed, averaged over a few instances, quantised and stored, and the
engine glues phones together at its own boundaries.

## What exists

```
output/ms_sam/voice/
  usd22.spd  usd22.sdf    22.05 kHz voice from the isolated diphone word list (2.4 MB)
  usd22_sent.spd          22.05 kHz voice from the 759-sentence corpus (1.7 MB)
  usd22_phase0.spd        the fully locked pulse build, kept for comparison
  LTTS1033.LXA r1033tts.LXA   Sam's lexicon and letter-to-sound data (needed at runtime)
output/ms_sam/samples/    our voices and real Sam saying the same texts
tools/ms_sam/bin/         sam.exe, sam.dll, sam_say.exe, samleaf.exe, samunit.exe (x64, w64devkit gcc)
tools/ms_sam/engine/      the decompiled engine source (MIT, no voice data)
```

Speaking needs only `voice/` and the binaries; `Sam.spd` is not needed at runtime.

```
tools\ms_sam\bin\sam.exe --data output\ms_sam\voice --voice usd22 "Hello." out.wav
```

## The two missing pieces

1. **Voice data.** `Sam.spd` is Microsoft's and is not in the repo or this project.  It comes from the
   Windows XP installation or from the still-hosted **Speech SDK 5.1** (`SpeechSDK51.exe`,
   <https://www.microsoft.com/en-us/download/details.aspx?id=10121>).  That installer is a ZIP holding
   `Data.Cab`; `expand -F:* Data.Cab <dir>` gives `Sam.SPD`, `Sam.SDF`, `LTTS1033.LXA` and
   `r1033tts.LXA`.  The builder uses `Sam.spd` as the source of the phone tables, CART and phone map and
   replaces only its synthesis section, so the result contains Microsoft's tables and is therefore a
   local-only derivative, like the Anna voice in the upstream repo.

2. **A SAPI/NVDA front end.** The repo is a plain C library, not a COM SAPI engine: there is no
   `ISpTTSEngine`/`ISpObjectToken` implementation and nothing registers a voice token.  SAPI 5 cannot load
   it as-is, and NVDA has no synth driver for it.  The library API in `engine/src/sam_tts.h` is already
   the right shape for a screen reader (PCM callback, word/sentence events, cancel, rate/volume/pitch),
   so the shortest route to NVDA is a synthDriver that loads `sam.dll`; the shortest route to SAPI is a
   COM wrapper DLL around the same API.  Neither is written yet.

## Building the voice

Needs Python with numpy and scipy, and `Sam.spd` (see above).

```
python tools\ms_sam\corpus_build.py --out output\ms_sam\build\corpus --rate 22050
copy output\ms_sam\build\samleaf.bin output\ms_sam\build\corpus\samleaf.bin
python tools\ms_sam\spdbuild_sam.py analyze --corpus output\ms_sam\build\corpus --rate 22050
python tools\ms_sam\spdbuild_sam.py build --corpus output\ms_sam\build\corpus --sam Sam.spd ^
       --out output\ms_sam\voice\usd22.spd --rate 22050 --gain 3.0
```

`samleaf.bin` is dumped once from the engine with `tools\ms_sam\bin\samleaf.exe Sam.spd samleaf.bin`
(it asks Sam's own tree for every phone/left/right/position combination).

The corpus exporter writes one unit per phone with the MFA boundaries, converts to the target rate,
and estimates pitch periods.  Voicing is decided per phone class: the nine voiceless consonants are
never voiced, vowels are searched only in the speaker's own range, and short gaps/octave jumps are
smoothed.  `analyze` aligns each phone to Sam's leaves (all four positions per instance), averages up to
six instances per unit and fills every other unit from the same phone's most typical recording.
`build` trains fresh LSF and excitation codebooks, quantises every frame and packs the `.spd`.

`--gain 3.0` brings the quiet source recordings up to Sam's level; without it the voice is about 12 dB
quiet.  `--fill-durations` gives every leaf the phone's measured median length, but for this corpus that
made speech 58% slower than Sam, so the default keeps Sam's own timing except for the 872 leaves that had
a matching recording.

## How well the recordings fit

Measured over the 1,666 line clips (4,349 phone instances, 22 minutes of audio):

| | |
| --- | --- |
| Sam's phone names | 52 (47 distinct phonemes, vowels split into stressed/unstressed) |
| our phone labels | 41 (all of Sam's consonants and stressed vowels; only 2 unstressed vowels) |
| units with a context-matching recording | 872 / 3,365 (25.9%) |
| units filled from the same phone's typical recording | 2,461 (73.1%) |
| units left empty -> synthesised silence | 32 (SIL and +NOISE+) |
| voice built | 2.4 MB, 80,717 frames (55,443 voiced) |

All phonemes are present, so nothing is missing and the engine never substitutes a different phone.
What is thin is **context**: our corpus is isolated citation-form words, so 74% of Sam's context-specific
units reuse the same take.  Anna's corpus in the upstream builder is 5.5 hours of continuous speech,
which is why her build could fill almost every leaf.

Two consequences are audible: the voice is about 12% slower than Sam on the same text (measured
durations come from isolated words), and its timbre comes from the recordings: long-term spectrum
distance to the source recording is 0.204 vs real Sam's 0.404, and a DTW comparison of the same word
("pea") favours our voice too.

## Sentence corpus (2026-10-29)

A second, much better-suited corpus: 759 sentences (38.1 minutes) recorded on a phone, already 22.05 kHz
mono and noise-reduced twice, one sentence per file.  `EnglishPrompts.txt` lists 3,538 prompts and the
first 759 match the files in order (duration vs prompt length correlates at 0.873).  Alignment with MFA
and the `english_us_arpa` model takes about 3 minutes.

| | word list | sentences |
| --- | --- | --- |
| recordings | 1,666 clips, 22.1 min | 759 sentences, 38.1 min |
| phone instances | 4,349 | 24,072 |
| units with a context-matching take | 872 / 3,365 (25.9%) | **3,025 / 3,365 (89.9%)** |
| after dropping poor takes | — | **3,081 / 3,365 (91.6%)** |

77 sentences were dropped (SNR below 6 dB, or alignment log-likelihood more than 2σ below the mean);
the remaining 682 still give 21,547 units.  The pitch tracker lost 25-40% of the frames inside vowels
on this noisier material, so `corpus_build.py` now fills a vowel's whole span from its detected voicing
(`fill_vowel`); vowel frames are 97-100% voiced and the paragraph's periodicity rose from 0.51 to 0.61.

The measured phone durations are natural rather than citation-form, so the sentence voice also fixes
the timing: 18.36 s for the sample paragraph against real Sam's 18.91 s (the word-list voice took 21.85 s).

Measured on the paragraph at gain 0.75: autocorrelation 0.60 (Sam 0.63), harmonic energy 0.805
(Sam 0.748), 14 clipped samples (Sam 27), RMS 2773 (Sam 3885).

`usd22_sent.spd` is this voice.  `samples/sent/compare_paragraph_word_sent_sam.wav` plays the word-list
voice, then the sentence voice, then real Sam, 0.6 s apart.

The alignment command (the environment's `mfa.exe` stub is broken, and `Library\bin` must be on PATH):

```
$env:PATH = "C:\diphone-mfa-env\Library\bin;C:\diphone-mfa-env\Scripts;" + $env:PATH
python -m montreal_forced_aligner align corpus english_us_arpa.dict english_us_arpa.zip sent_out ^
       --output_format long_textgrid --clean --single_speaker
python tools\ms_sam\corpus_build.py --segments corpus --textgrids sent_out ^
       --out sent\ancp --rate 22050 --skip bad.txt
```

`bad.txt` holds the basenames to exclude (from `sent_out\alignment_analysis.csv`).

## Pulse phase (the whisper fix, 2026-10-29)

The first build whispered on every voiced sound.  The cause was the stored **pulse**, not the filter:
the analysis pitch marks are period lengths, not glottal-closure instants, so each period was cut at a
drifting phase.  `pack_spectrum` re-centres the peak, but the fine phase differed from frame to frame,
and the decoded pulses of consecutive frames correlated only 0.0-0.2 (real Sam: 0.5-0.86).  The engine
replays those pulses as the voiced excitation, so the vowels and voiced consonants came out noisy.

`build --pulse smoothblend` (the default) first low-pass filters each unit's pulse phases over time (4
passes), then pulls them 55% of the way toward the unit's loudest pulse.  That keeps the real pulse shape
but lets it drift slowly, instead of freezing one pulse for the whole unit.  Consecutive-pulse
correlation, which is 0.05 in the raw analysis and 0.91 with `phase0`, lands at **0.85**; real Sam's own
units measure 0.67-0.86.  Measured on the sample paragraph:

| | real Sam | raw (`orig`) | `phase0` | default |
| --- | --- | --- | --- | --- |
| autocorrelation peak | 0.63 | 0.35 | 0.73 | **0.67** |
| harmonic energy ratio | 0.748 | 0.714 | 0.833 | **0.814** |
| consecutive-pulse correlation | 0.67-0.86 | 0.05 | 0.91 | **0.85** |
| clipped samples (21.9 s) | 27 | 21 | 3 | 7 |

Shorter smoothing (`--passes 1..3`) or a smaller `--blend` moves back toward the whisper; more of either
moves toward the robotic end.  `--pulse orig` reproduces the first, whispered build and `--pulse phase0`
the fully locked one; `output/ms_sam/voice/usd22_phase0.spd` is that build.
`samples/blend_ab/` has the same paragraph at several points on the dial.

## Quality ceiling

Sam's format is a 20th-order LPC vocoder with vector-quantised spectra at 22.05 kHz.  Even a perfect
corpus gives Sam's buzz; it cannot match the MBROLA diphone voice, which keeps the recorded transitions.
This path is worth it for the *front end* (Sam's normaliser, lexicon, tagger, prosody and the SAPI 4
effects), not for fidelity.

## Licensing

The engine source is MIT.  `Sam.spd`, `LTTS1033.LXA` and `r1033tts.LXA` are Microsoft's; the built
`usd22.spd`/`usd16.spd` contain Microsoft's phone tables, CART and lexicon data, so all of it stays on
this machine and is not redistributed.  The recordings are the project's own.
