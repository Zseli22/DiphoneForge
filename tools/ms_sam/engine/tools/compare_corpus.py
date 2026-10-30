"""Speak every corpus sentence with the C build and compare with the real engine's WAV."""
import os
import subprocess
import sys
import wave

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(ROOT, "build", "x64", "sam_say.exe")
CORPUS = os.path.join(ROOT, "ref", "corpus")

SENTENCES = {}
for i, line in enumerate([l.strip() for l in open(os.path.join(CORPUS, "sentences.txt"), encoding="utf-8") if l.strip()]):
    SENTENCES[f"s{i}"] = line
for i, line in enumerate(["I read the book yesterday. I will read it tomorrow.", "They live near the live wire.",
                          "Please record the record.", "The wind will wind the clock.",
                          "Hello there, how are you doing today?", "I cannot believe it; she said no: never again!"]):
    SENTENCES[f"s{20 + i}"] = line
for i, line in enumerate(["Blorfing zorblats don't care.", "I'm sure we'll see it's fine.",
                          "The NASA and FBI agents met Dr. Smith on Main St.", "Well-known e-mail addresses.",
                          'Some "quoted" words (in parentheses) here.']):
    SENTENCES[f"s{30 + i}"] = line


def rd(p):
    w = wave.open(p)
    return np.frombuffer(w.readframes(w.getnframes()), "<i2").astype(int)


def main():
    good = 0
    for k, text in SENTENCES.items():
        real = os.path.join(CORPUS, f"{k}.wav")
        if not os.path.exists(real):
            continue
        out = os.path.join(CORPUS, f"c_{k}.wav")
        tmp = os.path.join(ROOT, "ref", "tmp_text.txt")
        open(tmp, "w", encoding="utf-8").write(text)
        subprocess.run([EXE, "--data", os.path.join(ROOT, "data", "voice"), "@" + tmp, out], capture_output=True)
        a, b = rd(out), rd(real)
        n = min(len(a), len(b))
        ex = float(np.mean(a[:n] == b[:n])) if n else 0.0
        md = int(np.abs(a[:n] - b[:n]).max()) if n else -1
        ok = len(a) == len(b) and md <= 1
        good += ok
        print(f"{k:4} len C {len(a):7} real {len(b):7} exact {ex:.4f} maxdiff {md:6} {'OK' if ok else '<-- differs'}  {text[:60]}")
    print(f"{good}/{len(SENTENCES)} sentences match (+-1 LSB)")


if __name__ == "__main__":
    main()
