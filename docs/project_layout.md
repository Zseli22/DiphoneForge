# Project layout: repository, working directory, and what is private

The project exists in two layers. The **repository** is what other people get:
code, the finished voice databases, their audit data and a small reference
build, 39 MB in total. The **working directory** is this machine: the source
recordings and every intermediate needed to rebuild a database, roughly 700 MB.

Nothing in the second layer is committed, and `.gitignore` enforces it.

## Layer 1: committed

| Path | Size | Why |
| --- | --- | --- |
| `addon/` | 60 KB | NVDA add-on source: manifest, install tasks, driver, bridge, host, docs. |
| `voices/usd16/`, `voices/usd/` | 35.5 MB | The two databases plus descriptor, phtrans, metadata and provenance. |
| `docs/database/` | 644 KB | Audit data for the shipped voice: unit grid, take selection, validation, build report, gap analysis. |
| `fixtures/poc/` | 322 KB | Eleven-unit reference build used to test the toolchain. |
| `data/` | 4 KB | Phone inventory, eSpeak variants, onset and coda cluster lists. |
| `tools/*.py`, `tools/*.c` | <1 MB | The pipeline itself. |
| `tools/mbrolator-src/`, `tools/mbrola-src/` | ~4 MB | Third-party sources needed to rebuild the analysis tools and the streaming engine. |
| `docs/`, `AGENTS.md`, `roadmap.md`, `README.md` | 120 KB | Project knowledge and licensing. |
| `LICENSES/`, `NOTICE.md`, `LICENSE` | 90 KB | Licence texts and per-component terms. |

Not committed from `tools/`: `micromamba.exe` (10.8 MB), the extracted
`espeak-ng/` install (17 MB), `**/Obj/`, `mbrola-src/Bin/` and every `.exe`.
Those are local tool binaries, rebuilt from source or installed separately.

## Layer 2: local only

| Path | Size | Why it stays here |
| --- | --- | --- |
| `Voice_audio/` | 493 MB | Original 44.1 kHz stereo recordings. The master recordings can be turned into any voice, not just a diphone database. Never published. |
| `output/segments/` | 86 MB | Per-line 44.1 kHz clips and `segments.csv`. Derived audio, same reason. |
| `output/mfa_aligned/` | 2.0 MB | Phone TextGrids. Metadata, but only meaningful with the clips. |
| `output/unit_grid/` | 2.7 MB | Diphone labels, boundaries, diagnostics. A copy of the CSV is published in `docs/database/`. |
| `output/unit_quality/` | 322 KB | Audit results and pickup lists. |
| `output/mbrola_db/` | 64.9 MB | Build output: a second copy of both databases (identical to `voices/`), manifests, and intermediates. |
| `output/mbrola_poc/` | 3.2 MB | The eleven-unit proof of concept. The consumable part is now `fixtures/poc/`. |
| `output/pitch_test/`, `output/pitch_model/` | 13 MB | Closed pitch-model experiment. Reference only. |
| `output/ms_sam/` | 24.6 MB | Closed Microsoft Sam experiment. Reference only. |

`voice_file_ranges.txt` stays at the root: it maps each recording to its prompt
line range and is small, but it describes the private corpus rather than the
published voice.

## Regenerable

The build recreates `raw`, `ana`, `res`, `conv`, `SORTIE.*`, the `.seg` file and
the database from `output/unit_grid/unit_grid.csv`, `output/segments/` and
`Voice_audio/`, in about two minutes per rate:

```powershell
python tools\build_mbrola_db.py --rate 16000 --name usd16 --jobs 8
python tools\build_mbrola_db.py --rate 22050 --name usd  --jobs 8
```

Deleting the intermediates is safe once the databases and manifests are hashed
and the installed voices are re-tested. The databases themselves are not
regenerable from the repository alone, which is why they are committed.

## External and caches

- `tools/mfa-env` is a junction to `C:\diphone-mfa-env` (2.4 GB). The Montreal
  Forced Aligner runs from there; it is outside the project and git-ignored.
  Recreate it with:

  ```powershell
  tools\micromamba.exe create -p C:\diphone-mfa-env -c conda-forge --file tools\mfa-packages.txt
  ```

  MFA downloads the `english_us_arpa` acoustic model on first use.
- `tools/espeak-ng/` (17 MB) is used by `tools/phonemize.py` for prompt and
  coverage phonemisation. Keep it locally; it is not committed.
- `tools/mbrolator-src/` and `tools/mbrola-src/` are committed as sources. Their
  build outputs are not.

## Repeating the cleanup after a rebuild

```powershell
# Build intermediates (keep the databases and manifests)
Remove-Item output\mbrola_db\usd16\raw, output\mbrola_db\usd16\ana, output\mbrola_db\usd16\res, output\mbrola_db\usd16\conv -Recurse
Remove-Item output\mbrola_db\usd16\SORTIE.* -Force
Remove-Item output\mbrola_db\usd\raw, output\mbrola_db\usd\ana, output\mbrola_db\usd\res, output\mbrola_db\usd\conv -Recurse
Remove-Item output\mbrola_db\usd\SORTIE.* -Force
```

## MBROLA NG voices

`mbrolaNG` stores voices outside its add-on folder, so they survive add-on
updates:

```
%APPDATA%\nvda\mbrola_ng\voices\<id>\<id>
%APPDATA%\nvda\mbrola_ng\voices\<id>\<id>.voice
```

`tools/install_mbrola_ng_voice.py` writes both and updates `installed.json`.
The custom voices are installed as `usd16` and `usd`.
