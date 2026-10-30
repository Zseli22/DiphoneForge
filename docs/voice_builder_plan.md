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
| Map long recordings to prompt lines | `voice_file_ranges.txt`, `tools/resegment_from_whole_alignment.py` |
| Per-line clips | `output/segments/segments.csv` |
| MFA corpus and dictionary | `tools/make_mfa_corpus.py`, `tools/build_mfa_dictionary.py` |
| Phone alignment | Montreal Forced Aligner (external env) |
| Unit grid | `tools/build_unit_grid.py` |
| Audit and outliers | `tools/analyze_unit_grid.py`, `tools/validate_segments.py`, `tools/audit_units.py` |
| Database build | `tools/build_mbrola_db.py` |
| eSpeak integration | `tools/make_espeak_voice.py` |
| MBROLA NG integration | `tools/install_mbrola_ng_voice.py` |

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
  prompts/              carrier templates and example words
  mapping/
    espeak_phtrans.txt  frontend name -> database phone
    mbrola_ng.voice     phoneme map for MBROLA NG
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
- **MBROLA NG** has its own letter-to-sound, duration and pitch engine, but it
  only ships compiled language data for English (`en.dat`) and Croatian
  (`hr.dat`). It is a good second frontend for those languages, not a general
  path until its language packs can be produced.
- **Direct MBROLA** (`.pho` input) is useful for testing and for tools that
  already produce phone strings.

## Guided workflow

1. **New project.** Choose a language pack, a name, and the frontend targets.
2. **Prompts.** Generate the line list and show the expected phone coverage.
   Cluster prompts can be trimmed or expanded before recording.
3. **Import recordings.** Add one or more long WAVs. For each, enter the range
   of prompt lines it contains. Show the waveform, estimate line boundaries by
   silence, and let the user correct them. Export per-line clips with the same
   `source_start_s` mapping used now.
4. **Align.** Run MFA, show which lines failed or were flagged, and offer
   re-alignment or re-recording for those lines only.
5. **Build.** Select takes, derive word edges, splice the gaps, run the analysis
   chain, pack the database, and validate every unit.
6. **Install and compare.** Write the eSpeak table/descriptor or the MBROLA NG
   voice, then play a fixed word list and a paragraph through each frontend.

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
- MBROLA is AGPL-3.0 and voice licences vary. Recordings need the speaker's
  consent before distribution.
- A second frontend (MBROLA NG) means a second mapping to keep in sync. The
  database and manifest are shared; only the maps differ.
