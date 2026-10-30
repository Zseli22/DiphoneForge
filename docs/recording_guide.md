# Recording Guide for the Diphone Voice Database

This guide covers the practical steps for recording your own voice for the diphone synthesizer.

The goal is **not** to produce perfectly edited clips at this stage. You will record one (or a few) long audio files, and the computer will later cut them into diphone units using forced alignment.

---

## 1. Recording Format

Use the best quality your recorder supports:

- **File format:** Uncompressed WAV or PCM if possible. Avoid MP3 if you can — lossy compression leaves artifacts that make pitch analysis harder.
- **Sample rate:** 44,100 Hz (or 48,000 Hz; we can resample to 44.1 kHz later).
- **Bit depth:** 16-bit or 24-bit.
- **Channels:** Mono.

After transferring to your computer, keep the raw files untouched in a backup folder. Work only on copies.

---

## 2. Environment & Microphone Technique

- Record in the quietest room available. Turn off fans, air conditioning, and electronics that hum.
- Reduce echo: record in a room with soft furnishings, or hang blankets around the recording area.
- Keep the microphone a consistent distance from your mouth throughout the session (about 15–25 cm / 6–10 inches).
- Use a pop filter or windscreen if you have one. Plosives like `/p/` and `/b/` can cause clipping that is hard to fix later.
- Avoid moving your head while speaking. Consistent microphone angle matters.

---

## 3. Monitoring with Headphones

Your plan to use headphones so you can hear yourself is good. A few tips:

- Use **closed-back headphones** so sound from the headphones does not leak into the microphone.
- Keep the headphone volume low. You only need to hear yourself clearly, not loudly.
- Listen for:
  - Plosive pops and breath blasts.
  - Mouth clicks and lip smacks.
  - Room noise, hum, or outside sounds.
  - Inconsistent loudness.

---

## 4. How to Pitch Your Voice

You do **not** need to be perfectly monotone. Because the engine normalizes pitch afterward, the important things are:

- Pick a comfortable, natural pitch and stay roughly in that range.
- Avoid large pitch sweeps *within* a single syllable (e.g., do not intonate "baa" as a question).
- Avoid vocal fry (very low, creaky voice) and falsetto (very high, breathy voice).
- Try not to let your pitch drift downward as you get tired. Take breaks.
- A small amount of natural pitch variation is fine and even helpful; the analysis will extract an average F0 and normalize it.

You do not need to match a musical note or use a pitch guide.

---

## 5. Timing and Pace

Syllables do **not** need to be the same length.

- Speak at a natural, deliberate pace.
- Leave about **half a second to one second of silence** between each item.
- If you plan to say each item twice, leave a short pause between the two repetitions.
- Do not rush. Quality is more important than speed.
- Take a longer break every 50–100 lines. Drink water. Stretch.
- Avoid over-articulating or under-articulating. Read the prompts the way you would naturally say them.

---

## 6. The Recording Script

Generate a script with two repeats per item:

```bash
python tools/generate_recording_script.py --vowel-set full --repeats 2 --output-dir output
```

This produces a file at `output/recording_script.txt` with each line appearing twice, for example:

```text
0001. Say B AA
0001. Say B AA
0002. Say B AE
0002. Say B AE
```

The two repeats let you choose the cleaner take later.

You can print the script or have a screen reader read it. A teleprompter or large font is helpful so you are not shuffling paper near the microphone.

---

## 7. File Saving & Organization

- Save the recording as **one long file per session** (or split only if your recorder forces a file-size limit).
- Use a clear file name, for example:
  ```text
  voice_yourname_session1_2026-06-19.wav
  ```
- Keep a session log with notes:
  - Date and time.
  - Microphone / recorder used.
  - Room.
  - Anything unusual (a car passed, a cough, a restarted take).
- Record a few seconds of silence at the start of each file. This gives a noise sample for later processing.

Do **not** try to save each syllable as a separate file at this stage. That will be done automatically later.

---

## 8. Transfer and Initial Cleanup

After transferring the file to your computer:

1. Make a backup copy in a `raw/` folder and do not touch it again.
2. Trim only the **very beginning and end** of the recording to remove long leading or trailing silence.
3. If the recording is extremely long, you may split it into chunks of 200–300 lines each, but keep the line numbering consistent.

Do **not** manually cut out individual syllables. That is the job of the forced aligner in Phase 3.

---

## 9. Optional: Using a Display-Words Script

If reading "Say B AA" feels too artificial, you can generate a version that uses real words where available:

```bash
python tools/generate_recording_script.py \
    --display-words data/display_words.json.example \
    --output-dir output/words
```

The speaker script will then show words like "Say bee" where a good mapping exists, while the aligner still uses the phonetic token `CV_B_IY`.

You can also create your own `data/display_words.json` with more mappings.

---

## 10. Repeats, Mistakes, and the Transcript

The forced aligner works by matching the **transcript** to the **audio**. This means the number of times a syllable appears in the audio must match the number of times it appears in the transcript.

### If you record each item twice

Generate the script with `--repeats 2`:

```bash
python tools/generate_recording_script.py --vowel-set full --repeats 2 --output-dir output
```

Then speak exactly what the script shows:

```text
0001. Say B AA        (speak)
0001. Say B AA        (speak again, with a short pause)
0002. Say B AE        (speak)
0002. Say B AE        (speak again)
```

The aligner will find both utterances. Later you can choose the cleaner take.

### If you make a mistake

1. Stop speaking.
2. Leave a second or two of silence.
3. Repeat the same line the correct number of times.

After recording, edit the audio to remove the bad take, and also remove the matching line from `aligner_transcript.txt` and `diphone_map.json` so the counts stay in sync.

Do **not** insert unscripted extra repeats. If the audio has more utterances than the transcript, the aligner will become confused.

### One-take workflow

If you prefer simplicity, use the default one-take script:

```bash
python tools/generate_recording_script.py --vowel-set full --repeats 1 --output-dir output
```

This makes alignment the easiest because every transcript line appears exactly once in the audio. If you later dislike a particular take, record only those lines again in a short "pickups" file and provide a matching short transcript. You do not need to re-record the entire session.

### Simple rule

Whatever appears in the audio must appear in the transcript. Keep the two matched, and the alignment will work.

---

## 11. Quick Checklist Before Recording

- [ ] Recorder set to WAV, 44.1 kHz or 48 kHz, mono.
- [ ] Quiet room, mic positioned consistently.
- [ ] Headphones connected for monitoring.
- [ ] Recording script loaded and accessible.
- [ ] Water nearby; avoid sticky or crunchy food.
- [ ] Test recording made and listened to for plosives, noise, and level.
- [ ] Plan for breaks every 50–100 lines.

---

## 12. What Happens Next

Once the recording is done:

1. Transfer and back up the raw file(s).
2. Use the forced aligner (Phase 3) to find the exact start and end of each syllable.
3. The engine extracts diphone units, normalizes pitch, and builds the voice database.

You do not need perfect takes for every line. A few bad takes can be discarded or re-recorded later.
