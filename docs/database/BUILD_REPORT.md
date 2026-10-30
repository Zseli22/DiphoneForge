# Full MBROLA database build

Date: 2026-10-27
Builder: `tools/build_mbrola_db.py`
Speaker recordings: `Voice_audio/` (unchanged, read only)

## Result

| Item | Value |
| --- | --- |
| Database (NVDA, 16 kHz) | `output/mbrola_db/usd16/usd16`, 16 kHz, 15.6 MB |
| Database (NVDA, 22.05 kHz) | `output/mbrola_db/usd/usd`, 22.05 kHz, 21.6 MB |
| Units | 2,303 |
| Recorded diphones | 1,073 (one selected take per label, from 2,333 takes) |
| Word-edge units | 94 (`_X` and `X_` for all 47 phones) |
| Cross-spliced fills | 1,136 (the complete 47x47 pair matrix) |
| Build time | about 2 minutes each at 8 workers |
| Analysis failures | 0 |

`database_build` volume ratio is 0.8. At 1.0 MBROLA's overlap and transition
smoothing clipped a few isolated samples; 0.8 removes all clipping.

## 16 kHz and 22.05 kHz in NVDA

Both builds work. eSpeak 1.48 does not read the rate from the database header;
its voice parser takes an optional third field on the `mbrola` line and falls
back to 16000 when it is missing:

```
mbrola usd16 usd_phtrans 16000
mbrola usd   usd_phtrans 22050
```

Without that field the 22.05 kHz database generated 16 kHz audio while MBROLA
emitted 22.05 kHz samples, which played 1.375x slow and low (the earlier
measurement of 3.49 s / 76.7 Hz against 2.46 s / 99.4 Hz for `mb-en1`). The
stock 22.05 kHz voices (`mb-br2`, `mb-br3`, `mb-es2`) all include the field.

With the field, eSpeak sets its sample rate, emits its `SAMPLERATE` event with
22050, and the host opens the player at 22050. The 22.05 kHz voice measures
102.1 Hz against 101.3 Hz for the 16 kHz voice on the same text.

The 22.05 kHz build runs about 5-8% faster overall than the 16 kHz build for the
same text. Stock 22.05 kHz voices show the same shorter-consonant tendency
(`es1` 5.02 s of speech vs `es2` 4.85 s on the same Spanish sentence), so it is
eSpeak's consonant timing at the higher rate, not the database.

## Coverage

The recording plan covered CV, VC and the coda clusters that the top-10k words
use, but never all 2,209 phone pairs. The actual gaps were:

- 506 vowel-vowel pairs (VV was deliberately not recorded)
- 479 consonant-consonant pairs (most coda and cross-syllable clusters)
- 81 vowel-consonant pairs (including the 13 vowel+`r` pairs)
- 23 consonant-vowel pairs

MBROLA aborts the whole utterance on a missing pair (`Fatal error: Unknown
recovery for _-k segment`, exit -63), and eSpeak does not enable its
"replace with silence" mode. A partial database therefore cannot be evaluated
in NVDA, which is why every gap is filled.

Fills are built by crossfading the recorded second half of the left phone with
the recorded first half of the right phone, choosing the donor whose other
phone is articulatorily closest. Every fill is recorded in
`usd16/manifest.json` and `fallback_pairs.csv`.

How many gaps matter in real text: the top-10k English words use 293 of the
1,136 missing pairs (about 6% of all phone transitions). 843 gap pairs never
occur in that vocabulary. `gap_pairs_used.csv` lists the 293 with example
words, and `gap_recording_prompts.txt` gives a 244-word pickup list that
covers all of them; `gap_prompts_map.csv` maps each prompt to the pairs it
provides.

## Validation

- `tests/coverage_*.pho`: every diphone in the manifest synthesized in eight
  chunks, no fatal errors.
- `tests/word_*.pho`: `see`, `tap`, `pat`, `extras`, `further`, `computer`,
  `unnamed`, `bookkeeper`, `output`, `texts`, `asked`, `strengths`.
- Through the installed eSpeak voice: 30 s of stress text and a 48,874-character
  technical text (54 minutes of audio), no errors, peak 32323/32768, no clipped
  samples, longest silence 1.08 s (paragraph pause).

Per-unit output statistics are in `unit_stats.csv`. Natural and spliced units
have the same median level (-19.8 and -19.7 dBFS); word-edge units are quieter
because half of each is silence.

## eSpeak voice

`tools/make_espeak_voice.py` writes:

- `espeak-data/mbrola_ph/usd_phtrans`: 53 allophone sources, 65 records
  (`aa`->`a`, `O:`->`O`, `I#`->`I`, `a#`->`@`, `@2`->`@`, `i`->`i:`, `t#`->`t`,
  ...). Canonical names pass through unchanged. Format: 4-byte header, then
  24-byte records of six 4-byte fields: source name, next-phoneme condition,
  first output phone, second output phone, split percent, control bits. Phone
  names are packed as up to four ASCII bytes, not NUL-padded eight-byte strings.
  A two-phone record gives the first output `split` percent of the duration.
  Conditional records let a rhotic vowel drop its added r before a linking r.
- `espeak-data/voices/usd16` and `.../usd`: voice descriptors (`phonemes en-us`,
  `pitch 82 117`, `mbrola <db> usd_phtrans <rate>`). The trailing rate field is
  required for the 22.05 kHz voice.

The descriptor must live in the **voices root**, not `voices/mb/`: this eSpeak
build ignores newly added files in `voices/mb` (stock names work, new ones do
not), while a root-level descriptor is listed and selected normally.

Installed files:

- `%APPDATA%/nvda/addons/espeakWitMbrola/synthDrivers/espeak-data/mbrola/usd16`
- `.../espeak-data/mbrola/usd`
- `.../espeak-data/mbrola_ph/usd_phtrans` (shared by both voices)
- `.../espeak-data/voices/usd16`
- `.../espeak-data/voices/usd`

After restarting NVDA the voices appear as **us-mbrola-usd16** (16 kHz) and
**us-mbrola-usd** (22.05 kHz).

## Fixes after the listening passes

Three separate problems were found. All came from eSpeak's MBROLA phone names
differing from what the mnemonic dump shows; an unmapped name keeps its duration
but loses its sound.

First pass:

- Coda /l/ was silent in every context. eSpeak sends the name `l/2` for coda
  /l/ (the mnemonic dump still shows `l`). `l/2` was not in the table.
  Mapping `l/2` -> `l` fixed `well`, `feel`, `milk`, `pal`, `school`, `full`.
- `L`, `l/`, `l/3`, `l^`, `l.`, `l-`, `L/`, `r/`, `w/`, `m-`, `N-`, `d#`
  and `aI2`/`a/` are also mapped.

Second pass:

- The `@L`, `aI3` and `aU@` records were malformed. The writer used eight-byte
  name fields, so the second output phone was written into the split-percent
  field: `@L` became just `@`, `aI3` just `aI`, `aU@` just `aU`. This is why
  dark /l/ still vanished after schwa and why "fire" kept saying "fie".
  `tools/make_espeak_voice.py` now writes the engine's real six-field record.
  The layout was confirmed against `en1_phtrans`, whose `aI@` and `aU@` records
  use exactly this structure and do work.
- `@L` -> `@` + `l` (50/50): `table`, `channel`, `available`, `little`,
  `people`.
- `aI3` -> `aI` + `r` (70/30): `fire`, `require`, `tired`, `Ireland`.
- `aU@` -> `aU` + `r` (70/30): `flour`, `hour`, `sour`, `hourly`.
- `i@3` -> `i@` + `r` (70/30): `near`, `here`.
- When a vowel follows, eSpeak appends `r-` or `r` itself ("fire is" is
  `aI3 r- I z`). Conditional records drop the added r in that case, so it is
  not spoken twice.
- The synthesis phone list is not always the display list: `flower`, `player`
  and `employer` are synthesized as `aU`/`eI`/`OI` + `3`, where `3` is the
  recorded rhotic NURSE vowel. Those words already had an r-coloured tail. The
  `aU3`/`eI3`/`OI3` records are kept as safety mappings but are not used by
  en-us.

Verification: `channel` and `little` now end with F2 near 600-950 Hz instead of
1480-2030 Hz (dark /l/), and `fire` shows the F1 rise and F2 fall of a coda /r/.
`output/mbrola_db/tests/phonfix_words_usd16.wav` has the full word set.

`tools/espeak_phoneme_dump.c` (build with `gcc -m32`) prints the phoneme
mnemonics the installed 1.48 DLL produces. It shows display names; the actual
synthesis names have to be confirmed by substituting a mapping and listening to
or measuring the change.

## Stored-audio length and size

The database size is driven by how much audio each unit stores, not by the
number of units. 2,304 entries is already the minimum for the 47-phone
inventory: 47x47 pairs, 94 word-edge units and the silence diphone.

| Database | Units | Stored audio | Size |
| --- | --- | --- | --- |
| usd16 (accepted) | 2,304 | 487 s | 15.6 MB |
| en1 | 2,116 | 209 s | 6.7 MB |
| us1 | 2,025 | 225 s | 7.2 MB |

Units store the full midpoint-to-midpoint span, a median 210 ms each; en1
stores about 99 ms. MBROLA stretches each half to the duration it is asked for,
so the outer steady state is redundant. `--max-half-ms N` keeps at most N ms on
each side of the junction, preserving the transition, stop closure and burst.

A 60 ms trial build reached 9.29 MB with the same duration and peak and passed
all tests, but it was **rejected on listening**: speech became noticeably harder
to understand, so the full-length build is kept. Simulated sizes remain: 80 ms ->
11.0 MB, 100 ms -> 12.6 MB, 120 ms -> 13.5 MB.

MBROLA also supports replacement diphone entries that point at another entry's
samples. A pair can therefore exist with no sample cost. Applying that to the
1,136 spliced fills could save about 7 MB, but a duplicate reuses the donor's
audio for both halves, so the left phone would be wrong. It is not used while
the cross-spliced fills are preferred.

## NVDA driver change

`nvda-addon/synthDrivers/_espeak_mb.py` no longer opens a 22.05 kHz player at
startup. The player is created when the first audio packet arrives, using the
voice's real rate. Previously the first utterance after switching to a 16 kHz
voice could be cut while the player was swapped. The host readiness timeout was
also raised from 10 s to 30 s.

The first-load behaviour reported after installation (some speech missing until
the synthesizer was reloaded several times) could not be reproduced in a fresh
host process: 10 consecutive starts each listed all voices and accepted
`usd16`. It is most likely a one-time cache effect from installing the voice
while NVDA was running. A full NVDA restart clears it.

## Known limitations

- The 1,136 spliced fills are the weak part. Vowel-vowel fills are the most
  fragile: the left half ends leaving a consonant and the right half starts
  from a consonant, so the formant transition is synthetic. Consonant clusters
  and mid-word pairs are generally better.
- Take selection is automatic (SNR, MFA log-likelihood, edge-trim penalty,
  duration), not listening-based. A bad take in a frequent diphone can still
  surface.
- Only one take per label is in the database; there is no context-dependent
  variant selection yet.
- Word-edge units use 0.10 s of real room tone.
- Cross-word pairs from connected speech are not represented in the pickup
  list; they are only in the spliced set.

## Rebuild

```
python tools/build_mbrola_db.py --rate 16000 --name usd16 --jobs 8
python tools/build_mbrola_db.py --rate 22050 --name usd --jobs 8

python tools/make_espeak_voice.py --database usd16 --name us-mbrola-usd16 \
  --voice-file usd16 --table usd_phtrans --rate 16000 \
  --out output/mbrola_db/espeak_voice16
python tools/make_espeak_voice.py --database usd --name us-mbrola-usd \
  --table usd_phtrans --rate 22050
```

Then copy each database, the shared phone table, and both voice descriptors into
the add-on as listed above. The rate field must match the database rate.
