# eSpeak NG with MBROLA 3.0

First release of the add-on as part of DiphoneForge.

## Requirements

NVDA 2026.1 or later, 64-bit. The 64-bit to 32-bit bridge depends on that API, so
older releases will not be offered this version.

## What is in it

- eSpeak NG 1.51 (reports `1.52-dev`) driving a 32-bit MBROLA engine and a
  low-latency in-memory streaming build of MBROLA.
- **USD16**, a 16 kHz American English voice built from this project's own
  recordings, installed by default. 2,303 units, full 47x47 pair matrix.
- **USD**, the same recordings at 22.05 kHz, included as a proof of concept for
  higher-fidelity databases.
- Any other MBROLA database can be added by copying it into the voice folder; the
  voice list shows only databases that are present.

## Changes since the upstream 2.0 add-on

- eSpeak 1.48 removed entirely. Only eSpeak NG remains, and the add-on requires
  NVDA 2026.1 or later.
- The 13 bundled third-party databases are no longer shipped. Their licences
  forbid selling and restrict use to MBROLA; shipping none keeps the package
  clean and cuts its size by two thirds.
- Loudness: volume 100 now applies 2.4x gain through a soft-knee limiter, about
  4 dB above stock eSpeak, with no clipped samples.
- Index marks are attached to the audio that precedes them, so a mark is
  announced when its audio plays. Queued utterances run back to back.
- Interrupting speech can no longer resume part of the previous utterance.
- Voice updates no longer overwrite databases the user added.

## Licensing

The add-on code is GPL v2.0 and descends from NVDA's eSpeak driver and from
Felipe Porciuncula Zanabria's "Espeak with Mbrola included" add-on. The MBROLA
engine is AGPL v3.0, and the streaming engine is built from that source with one
small patch published in this repository. eSpeak NG and its data are GPL v3.0.
The two voice databases are CC BY 4.0. Full texts are in `LICENSES/`, and
per-component details are in `NOTICE.md`.

## Known limitations

- Capital pitch change is not supported by the MBROLA path. Use NVDA's
  "beep for capitals" setting.
- The 22.05 kHz voice is slightly faster than the 16 kHz one at the same rate
  setting, because eSpeak renders consonants shorter at higher rates. Stock
  22.05 kHz voices behave the same way.
- A third-party database must match the eSpeak voice descriptor's expected phone
  set; the standard MBROLA voices already do.
