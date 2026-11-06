# Toward a guided voice builder

The current pipeline works but is a collection of scripts with English names
and paths baked in. The aim is one program that takes a new speaker from
"here are my recordings" to "here is an installed voice", and that can be
pointed at another language without rewriting the pipeline.

## What already exists

| Stage | Tool |
| --- | --- |
| Phone inventory, clusters | `data/phonemes.json`, `data/onset_clusters.json`, `data/coda_clusters.json` |
| Prompt script, transcripts, dictionary | `tools/generate_recording_script.py` |
| Duplicate vowel detection | `tools/find_duplicate_vowels.py` |
| Map long recordings to prompt lines | `voice_file_ranges.txt`, `tools/resegment_from_whole_alignment.py` |
| Per-line clips | `output/segments/segments.csv` |
| MFA corpus and dictionary | `tools/make_mfa_corpus.py`, `tools/build_mfa_dictionary.py` |
| Phone alignment | Montreal Forced Aligner (external env) |
| Unit grid | `tools/build_unit_grid.py` |
| Audit and outliers | `tools/analyze_unit_grid.py`, `tools/validate_segments.py`, `tools/audit_units.py` |
| Database build | `tools/build_mbrola_db.py` |
| eSpeak integration | `tools/make_espeak_voice.py` |
| Prompt pronunciation reference | `tools/render_prompt_reference.py` |

The database builder and the audio analysis are already language-neutral. The
English-specific parts are the inventory, the prompt templates, the phoneme
names, and the frontend maps.

## Proposed project format

A voice project is a folder:

```
MyVoice/
  project.json          language, inventory, prompt count, frontend targets
  prompts/              generated lines, transcripts, dictionary
  recordings/           long WAVs as recorded
  ranges.txt            recording -> first/last line
  segments/             per-line clips + segments.csv
  aligned/              phone TextGrids
  grid/                 unit_grid.csv, audits
  build/                MBROLA database, manifest, report
```

`project.json` replaces the hard-coded `output/` paths. Everything else keeps
the current formats, so existing projects can be imported by writing one.

## Language pack

```
languages/en/
  language.json         code, name, eSpeak voice, MFA model, dictionary source
  phonemes.json         vowels, consonants, frontend variant names
  onsets.json           legal onset clusters
  codas.json            legal coda clusters
  prompts/              prompt templates, optional example words
  mapping/
    espeak_phtrans.txt  frontend name -> database phone
```

A language is usable when it has an inventory, a pronunciation source for the
aligner (MFA dictionary or eSpeak letter-to-sound), and a frontend map. The
recordings, alignment, grid and database stages do not care which language it
is.

## Frontends

The database is frontend-neutral. Only phone names and the mapping differ.

- **eSpeak** is the extensible choice. It supports every language whose eSpeak
  voice and dictionary exist, and a per-language `phtrans` plus a voice
  descriptor is all that is needed. This is the route for new languages.
- **Direct MBROLA** (`.pho` input) is useful for testing and for tools that
  already produce phone strings.

## Prompt pronunciation reference

The prompt text is the phone string the aligner needs, which is not something a
speaker can be expected to interpret. `0`, `S` against `s`, and `T` against `t`
carry no clue for a lay reader, and spelled keywords for every phone in every
pair make a script harder to read, not easier.

So the reference is audio, not text. The builder plays it in the prompt screen:
one line at a time, rendered on demand, with next, repeat and previous. Files are
a fallback for recording away from the machine, not the normal path:

```
prompts/ref/0001.wav ... 1666.wav   one file per prompt, for playback or another device
prompts/ref/index.json              line -> file, duration, phones
prompts/ref/track.wav               optional, the whole set in one file (--track)
```

Details that matter:

- The rendering uses eSpeak's phoneme input (`[[p i:]]`), never the printed
  prompt text. Reading `p i:` as text produces the letter names, which is the
  wrong sound.
- The vowel carries an explicit primary stress mark. eSpeak reduces an unstressed
  phoneme-input vowel: `[[p I]]` renders the happy vowel, not KIT, and only
  `[[p 'I]]` gives `ɪ`. Without the mark the reference would teach the wrong
  vowel for some prompts and nobody would notice.
- Pitch range is flattened (`-P 0`) so the speaker copies the phones instead of
  the melody. The rate stays at 150 wpm: below about 140 eSpeak stretches
  sustained vowels until they flutter, which is harder to imitate than normal
  speech. `-p` sets the base pitch so the reference sits near a comfortable
  speaking range; it is not matched to the speaker.
- Numbers are announced before each prompt only when asked for, because a
  spoken number is useful when following a track by ear and noise when watching
  the screen.
- The reference is stock eSpeak, not the finished voice. It tells the speaker
  which sounds to make and nothing about timbre, and the recording guide says so.
- Every render is checked against eSpeak's own phoneme echo. A phone that eSpeak
  substitutes is reported instead of being shipped silently; `0 r` is the one
  case in the current corpus, where American English turns the LOT vowel into
  THOUGHT before /r/.

## Duplicate vowels

An inventory that serves several varieties contains symbols that one variety
merges, and recording both halves wastes time. `tools/find_duplicate_vowels.py`
renders every vowel in a fixed consonant frame, compares the renders with DTW over
MFCC frames, and writes a merge map. US English yields `0 -> A:` (PALM and LOT are
the same waveform) and `3: -> 3` (NURSE, different tilt only), which removes 141
prompts from the 1,620.

The merge map is consumed twice: `generate_recording_script.py --vowel-merges`
drops the redundant targets, and `make_espeak_voice.py --vowel-merges` writes a
phtrans record so eSpeak can still emit the dropped symbol. The tool only finds
distinctions the synthesiser does not make; a dialect merge such as cot and caught
is a listening decision, and the report lists the near-threshold pairs for it.

## Guided workflow

1. **New project.** Choose a language pack, a name, and where the voice will be
   installed.
2. **Prompts.** Detect vowels the pack's voice cannot distinguish, merge them,
   then generate the line list and show the expected phone coverage. Cluster
   prompts can be trimmed or expanded before recording. Play each prompt through
   the pack's eSpeak voice from the prompt screen, rendered on demand.
3. **Import recordings.** Add one or more long WAVs. For each, enter the range
   of prompt lines it contains. Show the waveform, estimate line boundaries by
   silence, and let the user correct them. Export per-line clips with the same
   `source_start_s` mapping used now.
4. **Align.** Run MFA, show which lines failed or were flagged, and offer
   re-alignment or re-recording for those lines only.
5. **Build.** Select takes, derive word edges, splice the gaps, run the analysis
   chain, pack the database, and validate every unit.
6. **Install and compare.** Write the eSpeak table and descriptor, then play a
   fixed word list and a paragraph through the new voice.

The wizard should be wxPython and keyboard-accessible, with every action also
available as a CLI command so the work can be scripted.

## Phased plan

**Phase 0 - separate configuration from code.** Move the English inventory,
clusters, prompt templates and frontend maps into `languages/en/`. No behavior
change; the existing scripts read the same data.

**Phase 1 - one CLI entry point.** `voicebuilder init/import/segment/align/grid/build/install`
with a `--project` argument. Each command wraps an existing tool. `doctor`
reports missing MFA models, tools, recordings and ranges.

**Phase 2 - wizard.** A wxPython front end over the same commands. The first
useful screen is the recording/range editor, because that is the most manual
step today.

**Phase 3 - second language pilot.** Spanish or German is the sensible first
test: a smaller phone inventory than English, good MFA coverage, and eSpeak
support. Keep the inventory small (for example 25-30 phones) so the pair matrix
and the recording load stay reasonable.

## Language notes

- Pair coverage scales as N². English needs about 2,200 pairs; a 28-phone
  language needs about 780. A smaller inventory makes a full database much
  cheaper.
- MFA needs a per-language acoustic model and dictionary. Where no dictionary
  exists, eSpeak letter-to-sound output can seed one, as
  `build_mfa_dictionary.py` already does for English.
- Tone languages need pitch modelling beyond diphone concatenation; the
  frontend's pitch model matters much more than the database size.
- Scripts with no spaces, heavy morphology or many allophones need more work in
  the prompt and dictionary stages.

## Risks

- Alignment quality is still the main manual step. The audit tools catch gross
  errors, not every bad take.
- Cross-spliced fills are audible for some pairs. The pickup workflow removes
  them for the pairs that real text uses.
- The pronunciation reference is stock eSpeak. A speaker who imitates its timbre
  instead of its phones will drift away from their own natural voice, so the
  guide has to be explicit about what the reference is for.
- MBROLA is AGPL-3.0 and voice licences vary. Recordings need the speaker's
  consent before distribution.
