# Eleven-unit reference build

This is the smallest complete MBROLA build the project produces: eleven units
that make the words *see*, *tap* and *pat*. It exists so a change to the
analysis or packing stages can be checked without the private corpus.

Contents:

- `poc16.seg` — the MBROLATOR segmentation file. One record per unit: source
  audio, phone pair, first / transition / last sample.
- `raw/` — the eleven unit source files, mono signed 16-bit little-endian,
  16 kHz, named after their unit.
- `poc16.syn`, `poc16.f0`, `poc16.mbe` — the parameter files handed to the
  analysis tools.
- `poc16` — the packed database, 77 KB. This is the golden output.
- `manifest.json` — what the builder recorded about each unit.
- `pho/` — `.pho` input for the four test utterances.
- `wav/` — the expected audio for `see`, `tap`, `pat` and all of them together.
- `synth_results.json` — the validation run that produced those WAVs.

The analysis and packing stages (`anaf0`, `anambe`, `resynth`,
`database_build`) are described in `AGENTS.md`. They are not shipped here; they
are built from the sources in `tools/mbrolator-src`.

## How to use it

After changing anything in `tools/build_mbrola_poc.py`, or in the analysis
parameters, rebuild the eleven units and compare:

- the new database should contain the same eleven units with the same monotone
  phone labels and the same sample counts as `poc16`;
- the four rendered WAVs should match `wav/` in length and level, and be
  identical when the build is deterministic.

The units are quiet by design: room tone was used for the silence-adjacent
units, so peaks are low until the output gain is applied.
