# eSpeak-NG US English Phoneme Inventory

This project uses the same phoneme symbols that **eSpeak-NG** produces for General American English (`en-us`). Keeping the internal inventory aligned with eSpeak means the text-to-phoneme frontend is mostly a direct pass-through, with only a small normalization step for allophonic variants.

## Canonical vowels

| eSpeak | Example word | Notes |
|---|---|---|
| `i:` | fleece | long close front vowel |
| `I` | kit | short close front vowel |
| `E` | dress | short mid front vowel |
| `a` | trap | short open front vowel |
| `A:` | palm, start, father | long open back vowel |
| `0` | lot, cloth | short open-mid back vowel |
| `O` | thought | mid back rounded vowel |
| `V` | strut | open-mid central unrounded |
| `U` | foot | near-close back rounded |
| `u:` | goose | long close back rounded |
| `@` | schwa | unstressed neutral vowel |
| `3` | letter | rhotic schwa |
| `3:` | nurse | rhotic open-mid central vowel |
| `eI` | face | closing diphthong |
| `aI` | price | closing diphthong |
| `OI` | choice | closing diphthong |
| `aU` | mouth | closing diphthong |
| `oU` | goat | closing diphthong |
| `A@` | start, car | r-colored vowel |
| `O@` | north, force | r-colored vowel |
| `e@` | square, care | r-colored vowel |
| `i@` | near | r-colored vowel |
| `U@` | cure | r-colored vowel |

## Canonical consonants

| eSpeak | Example | eSpeak | Example |
|---|---|---|---|
| `p` | pen | `b` | bed |
| `t` | ten | `d` | dog |
| `k` | cat | `g` | go |
| `tS` | church | `dZ` | judge |
| `f` | fine | `v` | voice |
| `T` | thin | `D` | this |
| `s` | sit | `z` | zoo |
| `S` | ship | `Z` | measure |
| `h` | hat | | |
| `m` | map | `n` | nap |
| `N` | sing | | |
| `l` | leg | `r` | red |
| `w` | wet | `j` | yes |

## Allophonic variants mapped to canonical phonemes

eSpeak emits several context-dependent symbols. The frontend maps these to the canonical set above:

| Variant | Maps to | Reason |
|---|---|---|
| `I2` | `I` | unstressed variant of kit |
| `i` | `i:`` | unstressed happy vowel |
| `a#` | `@` | near-open schwa |
| `0#` | `0` | unstressed lot |
| `02` | `V` | one/what vowel |
| `E#`, `e#`, `e` | `E` | variants of dress |
| `a2` | `a` | trap that resists reduction |
| `@2`, `@5`, `@L`, `@-` | `@` | function-word schwas |
| `oU#` | `oU` | prefix vowel |
| `A#` | `A:` | British [a] → US [A:] |
| `O2` | `O` | thought variant |
| `i@3` | `i@` | mirror/nearer |
| `aI@` | `aI` | fire |
| `aU@` | `aU` | hour/power |
| `t#`, `t2` | `t` | flap / non-flap allophones |
| `w#` | `w` | voiceless wh |
| `r-` | `r` | linking r |
| `n-` | `n` | syllabic n |
| `?` | *(deleted)* | glottal stop, treated as silence for the first voice |

## Aligner-safe token names

Because eSpeak symbols like `:`, `@`, and `?` can confuse forced aligners, the recording-script generator creates safe token names (`ii`, `ei`, `AAr`, `ax`, etc.) for use in the aligner transcript and dictionary. The speaker-facing script still shows the real eSpeak symbols.

## References

- eSpeak-NG repository: `dictsource/en_rules`, `phsource/ph_english`, `phsource/ph_english_us`
- eSpeak-NG phoneme documentation: `docs/phonemes.md`
