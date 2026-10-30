# A pitch model that is not tied to an engine

This is the design note for `tools/pitch_model/`. It exists because the
intonation of one engine turned out to be small enough to describe, and the
description is useful on any engine that will take a contour. It says what the
model is, what an engine has to provide for it, and how much of the character
it actually carries.

**Scope.** Pitch only. Nothing here says anything about how samples are made:
no formants, no LPC, no unit selection, no voice conversion. The model answers
one question — given a sequence of phones and a few linguistic facts about
them, where does the voice's fundamental frequency go — and every engine below
is free to make sound however it likes.

## Why a breakpoint list, and not Fujisaki

A parametric pitch model (Fujisaki, Klatt's hat pattern, IPO) generates a
curve from a filter response: commands go in, a smooth continuous contour comes
out. Those are good models of *speech*, and they are the wrong shape for this
particular intonation.

The contour measured on Eloquence is a **piecewise-linear breakpoint list**.
Its own generator interpolates straight lines in whole numbers between stored
points; there is no filter anywhere in the path. Recorded from the running
engine, a 3.5 second sentence is **19 straight segments**, and a three-word
phrase is 9. The corners are where the character is:

- the accent is roughly a sawtooth: a rise of about 124 ms to a peak, then a
  straight fall to a floor;
- the peak sits 0.47 through the stressed vowel (quartiles 0.41 and 0.63);
- a declarative ends with two falls, the first anchored to the end of the last
  vowel, the second a reserved 58 ms ramp that lands 3 ms before the phrase
  ends;
- a question ends in a rise instead, and a comma ends high rather than low.

A second-order filter model rounds exactly those corners. That is also why
`fujisaki_style` in TGSpeechBox, good as it is, does not sound like this: the
curve family is wrong before any parameter is fitted. The same is true, less
severely, of a model built on exponential declination with IIR smoothing.

## What the model is

Five parts. Only the first is engine-specific.

1. **Events.** A list of syllabic nuclei with stress, plus word edges, phrase
   edges and a phrase type. Any engine can supply this or be made to.
2. **Placement.** Which nucleus is the nuclear accent, which are ordinary
   accents, which are unaccented. This is a policy, and it is the part that
   carries most of the identity.
3. **A grammar.** A small vocabulary of straight-line primitives — hold, rise,
   fall, reserved landing, question rise, continuation ending — in milliseconds.
4. **Two scalars.** Base pitch, and a range multiplier.
5. **A rate mapping.** The primitives are absolute durations, so they have to
   scale with the rate the engine is speaking at.

Levels are in semitones about the base pitch, so the same model works on a
70 Hz voice and a 220 Hz one. The numbers come from dividing the measured
contour by the source engine's 110 Hz default voice:

| level | semitones |
| --- | --- |
| start, first syllable accented | +1.7 |
| start, first syllable not accented | -0.9 |
| first accent peak | +3.0 |
| each later accent's peak | -0.35 each |
| floor after the first accent | -0.9 |
| each later accent's floor | -0.45 each |
| first of the two final falls | -5.7 |
| where a declarative lands (the model's own value; it measures -6.6 relative to the median) | -7.5 |
| where a comma phrase ends | -3.4 |
| where a yes/no question ends | +4.0 |
| rise | 100 ms |
| fall | 140 ms |
| peak position in the vowel | 0.47 |
| reserved landing | 58 ms, ending 3 ms before the phrase end |
| a pre-nuclear accent in a question | 0.15 of its ordinary height |

Those are the whole declarative inventory. It is worth saying plainly how small
that is: the "model" is a dozen numbers and four anchors.

The base pitch is chosen so the contour's **median** matches the original's,
not its mean. A contour that spends much of its time near a declining floor
sits below its own mean, and matching the mean then lifts the whole thing and
makes the landing too shallow — which is what the first version did.

## What an engine has to provide

For MBROLA this already exists — `.pho` takes `(position %, hertz)` pairs per
phoneme, any number of them, and interpolates linearly between points. Feeding
every breakpoint that falls inside a phone reproduces the polyline exactly.

A different engine needs the same two things and nothing more:

- **A phone timeline** it will actually speak: names and durations.
- **A way to set F0 over it**, either as breakpoints or as a per-phone level.

eSpeak is a special case worth knowing about: its intonation is *data*. The
tunes live in `phsource/intonation` and compile with `--compile-intonations`,
and they are already in the same family — `prehead`, `head`, `nucleus0`,
`nucleus`, with `fall` and `fall-rise` shapes. A new tune is a data change.
Changing *which syllable is the nucleus* is not; that lives in `intonation.c`.

TGSpeechBox would need a sixth `legacyPitchMode` that takes a breakpoint list,
or a mode that reads a table of measured contours. Fitting the existing
parametric modes is the wrong direction, for the reason above.

## Placement is where the character lives

The grammar above will make any engine sound *shaped* like this intonation.
It will not make it sound like the voice people recognise, because that comes
from decisions the grammar does not contain: which word in the clause is the
nuclear accent, how a word's class changes its prominence, how a list is
signalled, what a tag question does. In the source engine that is a large body
of word-class rules plus a nuclear-accent pass.

So the honest expectation for a port is "Eloquence-shaped prosody", not
"Eloquence". The current prototype takes the simple policy — every primary
stressed syllable gets an accent, the last one in the phrase takes the landing
— which is enough to hear whether the shape carries the character on a
different voice. If it does, the next investment is placement, and that is
engine-neutral work: it needs only stress, word class and position.

## Provenance

The constants are measurements of behaviour, re-expressed in semitones. No
table, rule body or code from any engine is copied, and none is needed: the
model is a dozen numbers and a polyline. That keeps it clean to publish, and
it is the reason an approximation is the right target rather than a risk —
cloning exactly would push toward copying tables, which would not fit another
engine anyway.

What is *not* claimed: that this reproduces any engine's output. It is a shape
with measured constants, and the ear is the only judge of whether it lands.

## What the recordings changed

Ten sentences read by the engine itself, and the same three at a much faster
rate, settled several things that had been guessed:

- **The accent rise is 100 ms, not 124, and it is not limited by the vowel.**
  Clamping it to the vowel's length turned every short syllable into a chirp;
  the model's rises came out at 35 ms against the reference's 100. This was
  the single largest cause of the model sounding forced.
- **The landing is right and the range is right.** The reference spans 9.7
  semitones on average, the model 9.8; it lands 6.7 below its median, the
  model 6.6. The absolute values were never the problem.
- **A question carries one accent, not four.** Eight- and five-word questions
  came back with a single accent each and about a semitone of movement
  elsewhere, then a long rise (yes/no) or a fall (wh). Accenting every
  stressed syllable through a question was the second largest error.
- **Several accents per word is correct.** A three-word sentence with long
  words carried four accents, so the contour is per stressed syllable after
  all, not one gesture per word.
- **A monosyllable can be just a fall**, with no accent peak at all.

What is still wrong, and known to be: the model accents more words than the
reference in short declaratives (3 against 2 on a six-word sentence), because
it has no notion of which words are given and which are new. That is the
placement problem, and it needs the text analysis rather than a constant.

## Using it

```
python tools/pitch_model/demo.py --name fox "The quick brown fox jumps."
python tools/pitch_model/demo.py --name q --end question "Is it raining?"
python tools/pitch_model/demo.py --name flat --range 0.7 "Same text, less melody."
```

`demo.py` asks the installed eSpeak for the phones and durations, runs the
model, and renders both contours through MBROLA, writing `<name>.base.wav` and
`<name>.model.wav` with identical durations. `pitch_model.py --debug --plot`
shows the events and draws the two contours against each other.
`measure.py` is a rough autocorrelation tracker for looking at the audio
itself; the `.pho` is the exact record.

The base pitch defaults to whatever makes the model's time-weighted mean match
the engine's own, so an A/B compares melody and not register. `--base` and
`--range` are the two knobs for tuning by ear.

## What is still missing

- **Rate.** The primitives are fixed milliseconds. At high rates a 124 ms rise
  eats a whole word, and the source engine keeps its shape with the segments
  compressed. A rate mapping is the next thing to add: scale the primitives
  with the vowel or the word, with floors.
- **Placement.** As above.
- **Multi-clause endings.** A list ("bread, milk, and eggs") is currently
  handled as comma-continuation plus a final landing. Real list intonation
  distinguishes the last item.
- **Emphasis and contrast.** Nothing here reads a caller's emphasis, and the
  source engine has one.
