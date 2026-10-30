# Third-party components and notices

This project redistributes other people's work. The table lists what is included,
under which terms, and where the corresponding source can be obtained.

## Included in the add-on

| Component | Version | Licence | Source | Modified here |
| --- | --- | --- | --- | --- |
| NVDA eSpeak synthesizer driver (`synthDrivers/espeakwithmbrola.py`, parts of `_espeak_mb.py`) | as of NVDA 2024.1 | GPL-2.0 | [nvaccess/nvda](https://github.com/nvaccess/nvda) | Yes, substantially |
| "Espeak with Mbrola included" add-on (`installTasks.py`, add-on structure) | 2.0 | GPL-2.0 | [FelipeZanabria/Espeak-wit-Mbrola](https://github.com/FelipeZanabria/Espeak-wit-Mbrola) | Yes |
| eSpeak NG engine (`espeak.dll`) | 1.51 X86, reports 1.52-dev | GPL-3.0-or-later | [espeak-ng/espeak-ng](https://github.com/espeak-ng/espeak-ng) | No |
| eSpeak NG data (`espeak-ng-data`) | 1.51 | GPL-3.0-or-later | as above | The Brazilian Portuguese and Spanish MBROLA phone translation tables carry corrections by Felipe Porciuncula Zanabria |
| MBROLA runtime (`mbrola.dll`, `mbrola.exe`) | 3.0.x | AGPL-3.0-or-later | [numediart/MBROLA](https://github.com/numediart/MBROLA) | No |
| MBROLA streaming engine (`mbrola_stream.dll`) | built from the above | AGPL-3.0-or-later | `tools/mbrola-src` in this repository | Yes: `close_InputFifo` is exported so the DLL build links |
| Native helper (`espeakMbrolaHost.exe`) | this project | GPL-2.0 | `addon/synthDrivers/espeak_mbrola_host.cpp` | - |
| USD16 and USD voice databases | 2026-10 | CC BY 4.0 | `voices/` in this repository | - |

## Present in the repository, not in the add-on

| Component | Licence | Notes |
| --- | --- | --- |
| MBROLATOR toolchain (`tools/mbrolator-src`) | AGPL notices in the source files; no top-level licence metadata upstream | Used offline to analyse and pack databases. Not distributed in the add-on. |
| Microsoft Sam research pipeline (`tools/ms_sam`) | MIT for the reconstruction, Microsoft data not included | Side experiment, unrelated to the add-on. |
| Corpora and derived data (`output/`, `Voice_audio/`) | CC BY 4.0 for this project's own recordings and derivatives | **Not distributed.** These directories are excluded from the repository; see README. |

## Not redistributed at all

MBROLA databases other than the two built here. Their terms permit free copying
and distribution when no charge is made, provided the notice travels with the
database, forbid selling them or bundling them into a sold product without the
author's permission, and restrict their use to MBROLA. Users install them
themselves; the add-on supports them without containing them.

## Source availability

The GPL and AGPL require that anyone who receives a binary can obtain its
source. For each binary above:

- The two native binaries built here are rebuilt by `build.ps1` from source that
  is in this repository, including the MBROLA patch.
- eSpeak NG and MBROLA are redistributed unmodified; their sources are at the
  upstream repositories linked above, at the matching version.
- The Python driver is source, shipped as source.

## Trademarks

eSpeak, eSpeak NG, MBROLA, NVDA and Windows are the names of their respective
projects and products. They are used here descriptively. This project is not
endorsed by NV Access, the eSpeak NG project, or the MBROLA project.
