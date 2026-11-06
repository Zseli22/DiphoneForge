# Recording Guide

This guide covers recording your own voice for a diphone voice database. It is
written for the person behind the microphone, not for the person writing the
pipeline code.

You do not need a phonetics background, a treated room, or expensive equipment.
You do need a quiet space, a consistent voice, and a recording you can map back
to the prompt list. Everything else is recoverable.

---

## 1. What you are recording, and why

The synthesiser is built from **diphones**: the sound of one phone moving into
the next. The `p` of "pat" gliding into the `a` is one diphone; the `a` gliding
into the `t` is another. A full voice needs every transition the language
actually uses, so the recording is a systematic sweep of them, not a reading of
prose.

Each prompt is therefore a short syllable, usually two or three phones:

```
p i:
t a p
s t r E
```

Two properties of that list matter:

- **The prompts are nonsense on purpose.** A real word carries its own stress,
  rhythm and spelling traps, and you would also spend time on sounds the
  language does not use in that position. A nonsense syllable has exactly one
  correct reading, which makes both your job and the aligner's job easier.
- **The symbols are for the computer.** `0` is a vowel, `S` and `s` are
  different consonants, and `T` is not a `t`. You are not expected to decode
  them. You are expected to say what the pronunciation reference says, and
  section 4 explains what that is.

A full US English set is about 1,600 lines. A language with 25 to 30 phones
needs far fewer, because the number of transitions grows with the square of the
inventory.

The list is generated from the language's inventory:

```bash
python tools/generate_recording_script.py --vowel-set full --output-dir output
```

That writes the speaker script, the aligner transcript, the dictionary and the
diphone map. Generate them once and keep all four together: the line numbers are
the identity of every take, and everything later refers back to them.

### Only record sounds that differ

An inventory written for compatibility with several varieties contains symbols
that are the same sound in yours. General American is a clear case: `A:` and `0`
are the PALM and LOT vowels, and the eSpeak voice renders them as literally the
same waveform; `3` and `3:` are both the NURSE vowel and differ only in spectral
tilt. Recording both halves of such a pair costs recording time and cannot
improve the voice, because the frontend cannot ask for a distinction the voice
does not make.

`tools/find_duplicate_vowels.py` finds these automatically. It renders every
vowel in a fixed consonant frame, compares the renders, and writes a merge map:

```bash
python tools/find_duplicate_vowels.py --write output/vowel_merges.json
```

For US English it reports `0 -> A:` and `3: -> 3`, which removes 141 of the 1,620
prompts. The same file feeds the prompt generator, which drops the redundant
targets, and the eSpeak table writer, which maps the dropped symbol to the vowel
you did record:

```bash
python tools/generate_recording_script.py --vowel-merges output/vowel_merges.json
```

The tool can only find distinctions the synthesiser does not make. A merge that
belongs to your dialect, such as cot and caught, is a listening decision; the
report lists the closest pairs just outside the threshold so you can make it.

---

## 2. Before you record

### Equipment

- A microphone you can keep at a fixed distance. A large-diaphragm condenser or
  a decent dynamic microphone both work; consistency matters more than cost.
- An interface or recorder that can write uncompressed audio.
- Closed-back headphones, or a single earbud, for monitoring.
- A pop filter or windscreen. Plosives like `p` and `b` are half of the corpus
  and are the easiest thing to ruin with a breath blast.

### Room

- The quietest room you have. Soft furnishings, curtains, a carpet and a bed
  help more than any plugin.
- Turn off fans, air conditioning, fridges, fluorescent ballasts and anything
  that hums. A fridge two rooms away is still audible at these levels.
- Avoid a room with parallel bare walls; echo cannot be removed later without
  damaging the voice.
- Put the microphone slightly off-axis from your mouth and angle the null of a
  cardioid pattern at your monitor and any equipment fan.

### Format and levels

- **Format:** uncompressed WAV. 44.1 kHz or 48 kHz, 16-bit or 24-bit, mono.
  Keep the same format for the whole project.
- **Levels:** aim for speech peaks around -6 dBFS. Never touch 0 dBFS; a clipped
  plosive cannot be repaired.
- **No processing while recording.** No compression, no limiter, no noise
  reduction, no EQ, no automatic gain. The analysis needs the raw signal, and
  processing that helps one unit hurts another.
- **Disable the operating system's "enhancements"**: automatic gain control,
  noise suppression, beam forming and echo cancellation all alter the signal in
  ways the pipeline cannot see.
- **Record a few seconds of room tone** at the start of every session. It is
  used to build the silence-to-phone units at word edges, so it should be the
  same room, the same microphone and the same gain as the speech.
- **Do not trim silence to zero.** Leave a second or two at the start and end of
  each file. The aligner uses the silence to find the first and last prompt.

---

## 3. Your voice

The database is one voice recorded consistently, over however many sessions it
takes, so the aim is not your best performance but your most repeatable one.

- **Use your normal speaking voice**, at your normal volume, at your normal
  distance from the microphone. Do not project like a stage actor and do not
  whisper.
- **Stay in one pitch range.** You do not need to be monotone, and small natural
  variation is fine, but avoid questions, exclamations and sighs. The prompts
  are neutral statements.
- **Do not copy the reference voice's pitch or melody.** The reference is a
  synthesiser, usually near-monotone. Copy its sounds, not its tune, and not its
  timbre.
- **Keep the articulation ordinary.** Over-articulating produces clean isolated
  sounds that sound wrong when joined; slurring produces units that cannot be
  cut. Say each item as you would say it inside a word.
- **Keep posture and head position steady.** Leaning in for a quiet line changes
  the tone and leaves a seam in the database.
- **Take breaks.** Fifty to a hundred lines, then stand up, drink water, and let
  your voice settle. Fatigue shows up as pitch drift and lazy consonants, and it
  shows up in the audit later.

---

## 4. Following the pronunciation reference

The prompt list is a phone string, which is not readable. The fix is audio, not
a written explanation.

`tools/render_prompt_reference.py` renders every prompt through eSpeak in the
language's own voice, with the melody flattened:

```bash
python tools/render_prompt_reference.py --play --voice en-us
```

The renderer runs at 150 words per minute by default. Slower is not better here:
below roughly 140 the eSpeak formant synthesis stretches sustained vowels until
they flutter, which is harder to imitate than normal speech. If 150 still sounds
unsteady, raise it towards the normal 175 rather than lowering it.

What the reference is:

- It is the exact sound sequence you are being asked to produce, so a symbol you
  cannot read stops mattering.
- It is a check on your own reading. If the reference says a short vowel and you
  were about to say a long one, you will hear it before you record it.

What the reference is not:

- It is **not the voice you are building**. It is stock eSpeak. Do not imitate
  its timbre, its pitch or its timing.
- It is **not a model of the finished speech**. The synthesiser supplies its own
  melody later; your job is to supply clean, stable sounds.

### Three ways to work with it

**Play in place (recommended).** `--play` renders each prompt in memory and plays
it, then waits for a key:

```text
[1/1620] line 0001: p i:
enter or space: next   r: repeat   p: previous   q: quit
```

You hear the prompt, say it into your recorder, and move on when you are ready.
Nothing is written to disk, and nothing plays while you are not looking at a
line. `--start-line 240` resumes where you stopped.

**Preview, then record.** Listen to a block of twenty to fifty prompts, then
record that block with nothing playing. The sounds are still fresh and there is
no chance of the reference leaking into the microphone. Use this when you record
with a separate device, or when the machine playing the reference is the same one
that is recording.

**Follow the track.** `--track` writes the whole set to one file with a gap after
each prompt, for playing on a phone or another device while you record. Speak
into the gap. `--announce` speaks the line number before each prompt, which is
useful when following the track by ear and resuming after a break. The track is
off by default because a 37-minute recording is the least flexible way to work.

If you follow along live rather than previewing, play the reference in one earbud
at low volume, keep the other ear free for your own voice, and use the earbud on
the side away from the microphone. Record thirty seconds and listen for a thin,
synthetic double of the reference underneath your voice; if you hear it, switch
to previewing. Never play the reference through speakers while recording.

The player is the same one the builder will use from the prompt screen. If you
use a screen reader, the line number and the prompt are printed before the audio,
so a re-read and a replay are one key each.

---

## 5. Pacing and phrasing

- Leave roughly **half a second to a second** between items. Long enough to
  separate them, short enough that you keep your place.
- Say the item once, cleanly. If you plan to say each item twice, use the
  `--repeats 2` option when generating the script so the transcript expects it.
- Do not insert unscripted repeats. The aligner expects exactly the prompts the
  transcript contains.
- Keep the pace steady across a block. Rushing the last ten lines of a block is
  the most common source of unusable takes.

---

## 6. Splitting a session into files

There is no required file layout. The pipeline does not care how many files you
produce or where you stop; it cares that it can tell **which prompt lines are in
which file**.

Three common approaches, all supported:

1. **One file per session.** Simplest to manage. A mistake late in the session
   means trimming back to the last good line and continuing in the same file, or
   starting a new file at that line.
2. **One file per block.** A checkpoint every fifty, hundred or few hundred
   lines. A mistake costs at most one block, and a long project can be spread
   over several days without losing track.
3. **One file per prompt.** Maximum isolation and the most work. It is only
   worth it for pickups of a handful of lines.

People who record live and fear mistakes usually prefer blocks. People who
record in one sitting usually prefer a single file. Both are fine, and you can
change approach mid-project.

### The range map

Each recording file is declared with the range of prompt lines it contains:

```
# filename  first_line  last_line
session1.wav   1   240
session2.wav   241 512
```

The rule that matters: **every prompt line appears exactly once across all
files, once and only once, with no gaps and no overlaps.** This map is what lets
the pipeline find line 812 three months later.

### A worked example

You are recording into `session2.wav`, which should cover lines 241-512. At line
400 you fluff the take.

- **Fix in place.** Stop. Trim the file back to the end of line 399. Resume
  recording at line 400 in the same file, or save what you have and start
  `session3.wav` at line 400. Either way the map stays exact: 241-512 in one
  file, or 241-399 and 400-512 across two.
- **Keep going.** Note the number 400, finish the block, and re-record line 400
  later as a pickup. The map still says 241-512, because the file still contains
  a take of every line in that range.

What you must not do:

- Leave a line out because it was difficult.
- Say a line twice and hope the aligner sorts it out. It will not; it will align
  both takes to one transcript entry and produce a unit cut across the gap.
- Renumber the script to match what you actually did.
- Hand-edit the transcript or the diphone map to remove a line. Edit the audio
  and the range map instead; they are the two things the rest of the pipeline
  reads.

A mismatch between audio and map is the most expensive mistake in the whole
project. It does not usually fail loudly: a wrong split silently poisons an
entire block of units, and you only find out when the voice sounds broken. This
is why the builder validates the map before it accepts any recordings.

### If your recorder splits files for you

File-size limits on portable recorders are fine. Treat each resulting file as
its own range and record where you stopped. Just write down the line number at
the moment of the split, or the importer will have to guess.

---

## 7. Mistakes and bad takes

A fluffed take is normal and not a reason to start over. There are two workable
responses, described in section 6, and both leave a valid map.

- **Coughs, clicks, sirens, a car outside:** stop, wait for silence, and re-say
  the line. If the noise covers a whole block, finish the session and re-record
  that block as a pickup.
- **A word said with the wrong vowel, or a stumble:** re-say the line
  immediately. Do not try to fix it in software.
- **A whole block that feels wrong:** keep it, mark it, and re-record it as a
  pickup. Re-recording is cheap; a voice built from tired takes is not.

Write the line numbers of anything you are unsure about. A list of forty numbers
is a ten-minute pickup session; discovering the problem during the audit is a
week of wondering what went wrong.

---

## 8. Files, names and backups

- **Name files predictably**, for example `s01.wav`, `s02.wav`, or
  `voice_name_session_2026-05-01.wav`. No spaces, no special characters.
- **Keep a session log**: date, microphone, interface gain, room, anything
  unusual, and the line range of each file. The range map is the machine-readable
  part; the log is for you.
- **Back up the originals immediately** and treat them as read-only. Work from
  copies, and never apply noise reduction, normalisation or editing to the
  original.
- **Keep the format identical** across sessions. If you change sample rate,
  bit depth or microphone halfway through, say so in the log; mixing formats is
  recoverable but it leaves seams that are hard to hear and easy to blame on the
  synthesiser.
- **Do not move or rename files after you have declared the ranges** without
  updating the map. The map refers to filenames, not to recording order.

---

## 9. Accessibility

The recording workflow is entirely keyboard-usable and does not require reading
a waveform.

- The prompt list is plain text, one numbered line per item, so any screen reader
  can read it, and a braille display or large font works equally well.
- The pronunciation reference is audio, so the symbols on screen never need to be
  interpreted. If you are following the track by ear, use `--announce` to hear
  the line number before each prompt.
- If you record with the prompt on screen, put the play control on a single key
  and keep the prompt list in a window the screen reader can re-read on demand.
- Teleprompter software, a second screen, or a phone showing the track are all
  fine; none of them touch the recorded audio.

---

## 10. Pickups and later re-records

Later re-records use their own short prompt list and their own numbering,
rather than new lines in the main map:

- Take the flagged line numbers from the audit, or the list you kept while
  recording.
- Generate a pickup prompt list for just those lines, with a matching transcript
  and diphone map.
- Record it in a fresh file and declare that file against the pickup list.

A pickup file that covers scattered lines is normal; the pickup list is what
makes it expressible. The legacy route is `tools/generate_rerecord_script.py`,
which turns old unit-review verdicts into a pickup list; the builder will derive
pickups from the audit directly.

---

## 11. What happens next

Once the files and the ranges exist:

1. **Segmentation** cuts each line into its own clip and records where it came
   from in the original file.
2. **Alignment** matches each clip against its transcript and finds the phone
   boundaries. Lines where the audio and transcript disagree are flagged rather
   than guessed at.
3. **Audit** checks each candidate unit for clipping, displaced speech, silence
   at the wrong end, and a unit that does not fit its neighbours.
4. **Build** selects one take per unit, derives the word-edge units from room
   tone, fills the pairs that were never recorded by splicing, and packs the
   database.
5. **Listening** is the final judge. Expect a short pickup list after the first
   build; that is normal, and it is a much better use of a session than
   re-recording everything.

You do not need perfect takes for every line. You need an honest map, a stable
voice, and a quiet room.

---

## 12. Before you start: checklist

- [ ] Prompt list generated, and the number of lines noted.
- [ ] Pronunciation reference rendered and checked on the first ten prompts.
- [ ] Recorder set to WAV, mono, 44.1 or 48 kHz, 16 or 24-bit.
- [ ] Interface gain set so speech peaks near -6 dBFS; no processing enabled.
- [ ] Room tone recorded at the start of the session.
- [ ] Headphones or single earbud ready for the reference.
- [ ] Microphone distance fixed, pop filter in place.
- [ ] Range map open, with the line number you are starting from.
- [ ] Water nearby, breaks planned, session log started.
