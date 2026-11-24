# Hybrid Word-Corpus Pipeline & Multi-Language Architecture

This document records the proven real-word + minimal-pair MBROLA voice-building workflow validated on UK English (`ukd16` / `ukd`), along with the exact contracts and invariants needed to abstract it across languages and wrap it in a keyboard-accessible GUI wizard.

---

## 1. Why Hybrid Real Words Beat Monotone Nonsense Lists

Measured comparison on the same speaker & recording equipment:

| Metric | Old US Nonsense Script (`usd16`) | New UK Hybrid Word Script (`ukd16`) |
| --- | --- | --- |
| Prompt count | 1,666 isolated syllables | **474** (434 real words + 40 logatomes) |
| Recording session length | > 60 minutes (19 WAVs) | **15.9 minutes** (1 WAV) |
| Unique recorded internal diphones | 1,073 | **1,002** (+ 88 word edges) |
| Natural CC clusters / VV pairs | 3 coda clusters / 0 VV | **190 CC clusters / 50 VV pairs** |
| Real-text running-speech coverage | ~94% (293 top-10k gaps) | **~99.5%** (gaps are mostly impossible/rare pairs) |
| 16 kHz / 22.05 kHz database size | 15.6 MB / 21.6 MB | **10.79 MB / 15.31 MB** (30% smaller, natural lengths) |
| Speaker F0 handling | Hardcoded 122 Hz (`120` / `180`) | **Auto-measured** (`101.6 Hz` → `158` / `218`) |

---

## 2. End-to-End Stage Architecture (GUI & CLI Contract)

Every stage reads/writes plain JSON/CSV/WAV files inside a self-contained project folder so any stage can be re-run idempotently from either a CLI or a wxPython wizard screen:

```text
<project>/
  project.json             language/voice code, target rates, speaker F0, stage status
  plan/
    inventory.json         canonical vowels/consonants, IPA map, eSpeak normalizations
    diphone_map.json       ordered lines (real words + supplement logatomes, phones, IPA, stresses)
    recording_script.txt   speaker-facing prompt list
    coverage_curve.csv     word-by-word running-speech & unique-diphone coverage
    plan_report.md         human-readable summary
  ref/                     optional eSpeak reference audio (on-demand --play or per-line WAVs/track)
  recordings/              untouched source WAVs (external recorder) OR direct per-line takes (PC mic)
  align/
    token_meta.json        unique prompt token -> canonical phone spans
    mfa_dict.txt           aligner dictionary (IPA or model-specific phone set)
    aligned_whole/         Pass-1 whole-file TextGrids (external recorder path only)
    segments/              per-line 44.1 kHz clips + segments.csv (with source_start_s!)
    mfa_aligned/           Pass-2 per-line TextGrids
  build/
    unit_grid.csv          all extracted diphone candidates + SNR/RMS/F0/position scores
    take_selection.csv     winning take per recorded diphone
    <name>/                <name> (MBROLA DB), <phtrans>, mb-<name>, manifest.json
    build_summary_<name>.json
    validation.json        full coverage validation through mbrola.exe
  samples/                 synthesized listening WAVs via espeakMbrolaHost.exe
```

### Stage 1 — Language Bootstrap & Hybrid Prompt Planning (`tools/plan_word_corpus.py`)
- **Inputs:** An eSpeak NG voice code (e.g. `en-gb`, `hu`, `es`, `hr`) + a plain-text frequency word list (`word [count]` per line, ~400 KB, e.g. MIT-licensed `hermitdave/FrequencyWords`). Zero third-party Python packages required.
- **Discovery:** Batch-phonemizes the vocabulary through `espeak-ng -v <voice> -q -x --sep=_` and `--ipa --sep=_` to obtain 1-to-1 aligned eSpeak symbols, lexical stress, and IPA.
- **Automatic cleanliness filters:**
  - Rejects spelled-out acronyms (`len(phones) > len(word) + 1`, missing orthographic vowel letter, `;` letter-spelling).
  - Excludes heterographs and dialect-ambiguous words automatically via `--skip-symbols` (e.g. `aa` BATH words in `en-gb`) and `--compare-voice` (e.g. comparing `en-gb` vs `en-gb-x-rp` excluded 366 disagreeing words with zero manual curation).
  - Run `tools/find_duplicate_vowels.py --voice <voice>` to confirm which eSpeak vowel symbols are acoustics-identical in that language.
- **Greedy set cover:** Weights diphones sublinearly by real-text frequency (`log1p(freq)^1.3`), prefers stressed and word-medial occurrences (`+0.25` / `+0.15`), penalizes unstressed word-final tails (`-0.15`), and favors familiar shorter words.
- **Minimal-pair supplement tail:** Only real-text gaps missed by the words are appended at the end. Consonant-consonant gaps are wrapped in clear two-syllable logatomes (`a C1 C2 @`, e.g. `a l S @`) so eSpeak renders them clearly without reducing the first vowel.

### Stage 2 — Dual Recording Workflows (Converging on `segments/`)
Both workflows produce the exact same per-line clips + `segments.csv` (`source_wav`, `source_start_s`, `source_end_s`):
1. **External recorder import:** User drops long WAV(s) + a strictly validated range map (every prompt once, no gaps, no overlaps) → Pass-1 whole-file alignment → automatic outer-silence trimming → per-line clip export.
2. **Built-in keyboard-accessible recorder (future GUI screen):** Plays eSpeak reference on demand (`--play` logic), records one prompt directly to a per-line WAV, runs instant clipping/SNR/duration check, and lets the user press a single key to accept or retake — skipping whole-file slicing completely.

**Pronunciation reference (`tools/render_prompt_reference.py`):**
- Uses mild natural pitch range (`--range 30`) rather than flat monotone (`-P 0`).
- Spoken line numbers (`--announce`) stay opt-in (off by default) and include a 350 ms gap before the prompt when enabled.
- `--only-supplements` renders or plays just the small nonsense tail so speakers never have to parse phonetic symbols.

**Speaker delivery rule:** Neutral "comma-list" tone (relaxed natural articulation, steady vocal effort, no sharp citation-form pitch drop or vocal fry at word ends).

### Stage 3 — Pluggable Forced Alignment & Integrity Verification (`tools/align_word_corpus.py`)
- Subcommands: `prepare` (strict range-map validator + MFA IPA dictionary), `align1` (Pass-1 whole-file MFA), `verify` (VAD-vs-MFA integrity check), `slice` (outer silence-leak trimming + `segments.csv`), and `align2` (Pass-2 per-line MFA).
- **IPA-first acoustic models:** Prefer MFA 3.x IPA models (`english_mfa`, `spanish_mfa`, `croatian_mfa`, `german_mfa`, etc.).
- **Mandatory model phone-set check:** Always inspect `mfa model inspect acoustic <model>` and assert every dictionary symbol exists in the model's exact phone inventory (for example, `english_mfa` v3.1.0 uses `ɐ`, `ej`, `aj`, `ɔj`, `aw`, `əw`, `oː`, `ɛː` rather than `ʌ`, `eɪ`, `aɪ`, `ɔː`).
- **Pass-1 integrity check (VAD vs. MFA):** Before cutting clips, verify:
  - Every prompt aligned (`aligned_words == expected_prompts`) with exact phone counts.
  - Every aligned word has real speech energy well above the room-tone floor (catches skipped prompts).
  - Report any unaligned speech islands (catches inline retakes/extra speech).
  - Trim Pass-1 outer silence leaks (quiet leading `ð` or trailing stops/vowels occasionally soak up adjacent silence in whole-file alignment) using frame RMS before running Pass-2 per-line alignment.
- **Pluggable backend hook:** Keep alignment behind a single contract (`clips + expected phones -> per-line TextGrids`). For languages lacking an MFA acoustic model (e.g. if Hungarian needs an alternative), a multilingual CTC aligner (such as Meta MMS forced aligner via ONNX/PyTorch) can slot into the exact same stage.

### Stage 4 — Speaker- & Language-Neutral MBROLA Build (`tools/build_word_mbrola_db.py`)
- **Word-aware & familiarity-weighted take scoring:** Scores every diphone take across the corpus using SNR, phone durations, peak headroom, RMS consistency, F0 stability & proximity to speaker median F0, word-medial/stressed bonuses, and a **language-neutral `vocab_rank` familiarity bonus** (so common everyday words like `museum`, `subtitles`, `beer` win over obscure subtitle tail names like `guo`, `shaolin`, `sophia` without any English-specific rules).
- **Universal IPA donor distance:** Derives articulatory vowel (height, backness, rounding, length) and consonant (place, manner, voicing) features directly from `inventory.json["ipa_map"]` so cross-spliced matrix fills work for any language without hand-authored tables.
- **Auto speaker F0 & pitch range:** Measures median F0 across all aligned voiced vowels and derives `period`, `FrameShift`, `FrameLength`, gender, and eSpeak descriptor `pitch <low> <high>`.

---

## 3. Hard-Won Technical Invariants (Must Preserve in Any Abstraction)

1. **`anambe.exe` requires EVEN `period` / `FrameShift` / `FrameLength`:** Odd values (e.g. `157` / `471`) crash `anambe.exe` with `0xC000001D`. Always round `period = max(40, round(rate / median_f0 / 2) * 2)`, `FrameShift = period`, `FrameLength = min(960, 3 * period)`.
2. **Low-F0 22.05 kHz MBROLATOR patch (essential for relaxed male voices < 110 Hz):**
   - Upstream `anaf0` (`pitchana.cpp`) hardcoded `MaxPer_8000 = 100` (200-sample max lag = **110.25 Hz floor at 22.05 kHz**!), causing pitch clamping and crackling on ~100 Hz voices at 22.05 kHz. Patched to `MaxPer_8000 = 150` (down to ~73.5 Hz at 22.05 kHz) with `FrameLength = 640` in `.f0` for rates > 16 kHz.
   - Upstream `anambe` (`MaxRaie = 80`) and `resynth` (`MAX_HARMO = 80`, `char line[1024]`) truncated harmonics above #80 at `period > 160` and truncated long `.mbe` lines at byte 1024. Patched to `140` harmonics (extended `PhaseResynth[140]`) and `char line[8192]`.
3. **`resynth.exe` minimum span guard:** Ultra-short diphone spans (< ~7 periods, e.g. ~55 ms) can segfault `resynth.exe` (`0xC0000005`). Clamp analysis spans to at least `8 * period` using the surrounding `PAD` context and retry with wider context if needed.
3. **22.05 kHz MBROLATOR rule:** Always keep `Freq=16000` inside `.f0`, `.mbe`, and `.syn`; scale `period`/`FrameShift`/`FrameLength` to the speaker's F0 at `22050`, and pass `22050` only to `database_build.exe` and the eSpeak descriptor's `mbrola` line.
4. **`.seg` column order:** `file  left  right  first  LAST  MIDDLE` (`last` precedes `middle`).
5. **No consecutive `_` pauses in validation `.pho` files:** Back-to-back `_` lines in synthetic coverage `.pho` chunks can crash `mbrola.exe`.
6. **eSpeak NG phoneme table names:** Some voice codes differ from their phoneme table name — notably British English uses `phonemes en` (not `phonemes en-gb`, which prints `Unknown phoneme table`). Verify every new language with `tools/smoke_mbrola_host.py`.
7. **Keep `phtrans` faithful to eSpeak — and guard against unrecorded loanword/allophone phones:**
   - Map eSpeak's allophones and multi-phone splits faithfully (e.g. stock `en1_phtrans` rules) rather than manually remapping unrelated phonemes, which causes accent bleed-through across words.
   - **Multilingual `phtrans` caveat (affects all non-English languages):** At runtime, eSpeak NG can emit phonemes absent from a language's recorded canonical inventory—most commonly when switching `(en)` for English loanwords/brand names, encountering rare loanword/onomatopoeic sounds (e.g., `dZ`, `tS`, `E:`, `g`, French nasal vowels), or emitting rare language-specific allophones. Because MBROLA aborts on any unknown diphone, `<db>_phtrans` must map every unrecorded eSpeak phone into the recorded canonical set. `write_espeak_voice_files()` now includes a universal fallback table covering common prosodic markers, allophones, French nasals, and English-switch phonemes, but languages with unusual script-specific eSpeak symbols (e.g. Slavic palatals, tonal/retroflex variants, or click/trill allophones not yet in that table) should still be checked against eSpeak NG's stock `espeak-ng-data/mbrola_ph/<lang>_phtrans` (or tested over real text with `tools/smoke_mbrola_host.py`). Longer-term, the clean automatic fix is to scan the target language's full eSpeak phoneme set (or stock `<lang>_phtrans` + nearest-IPA mapping) when generating `<db>_phtrans`, rather than trying to force speakers to record rare loanword phonemes or filtering them out of the corpus.
8. **Avoid `librosa` for simple F0/RMS scans:** Pure-NumPy FFT autocorrelation + `scipy.signal.resample_poly` runs in seconds with zero `numba` JIT stall risk.

---

## 4. Checklist for Adding a New Language (e.g. `hu`, `es`, `hr`)

1. **Word list:** Obtain a plain-text top-20k frequency list (`output/wordlists/<lang>_50k.txt`, e.g. from `hermitdave/FrequencyWords`).
2. **Inventory & merges:** Run `plan_word_corpus.py --voice <lang> --wordlist ...` to auto-discover raw eSpeak phones + IPA, inspect stock `<lang>_phtrans` (if one exists in `espeak-ng-data/mbrola_ph/`) for multi-phone splits, and run `find_duplicate_vowels.py --voice <lang>` to drop indistinguishable eSpeak vowels.
3. **Aligner mapping:** Inspect the target MFA IPA model (`mfa model inspect acoustic <model>`) and define the small `espeak_phone -> model_ipa` table (or use the CTC backend).
4. **Record & verify:** Generate the hybrid script (~300–500 prompts), record via external recorder or built-in mic recorder, run Pass-1 + VAD integrity check → Pass-2 alignment.
5. **Build & test:** Run `build_word_mbrola_db.py` for 16 kHz (and optionally 22.05 kHz), validate 100% of matrix diphones, and synthesize test sentences via `tools/smoke_mbrola_host.py`.

---

## 5. Planned wxPython Wizard Screens (Keyboard-Accessible)

Each screen calls one underlying Python module (no pipeline logic duplicated in UI code):
1. **Project Setup:** Pick language/eSpeak voice, voice ID, sample rate(s), and word list → generates `plan/` and shows coverage curve.
2. **Prompt Studio (Dual Mode):**
   - *PC Mic mode:* Keyboard loop (`Space` listen/record, instant clipping/SNR verdict, `R` retake, `Enter` next).
   - *External Recorder mode:* Export script & optional reference audio, then import WAV(s) + range map with strict gap/overlap validation.
3. **Align & Audit:** Runs Pass-1 integrity check + Pass-2 alignment, highlights only flagged lines (retake/re-align queue).
4. **Build & Listen:** Auto-F0 build → full MBROLA validation → built-in playback box synthesizing arbitrary test text through `espeakMbrolaHost.exe` + one-click export to `voices/<id>/` or NVDA's add-on folder.
