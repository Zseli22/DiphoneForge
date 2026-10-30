"""Compare morph.py (lexicon -> morphology -> LTS chain of FUN_5ed4b1f9) with engine lookups."""
import collections
import sys

import lookup_log
import lts
import lxa
import morph

LX = "../extracted/WinXP_TTS_Voices/$COMMONFILES/SpeechEngines/Microsoft/Lexicon/1033/"
LEX = lxa.Lexicon(LX + "ltts1033.lxa")
LT = lts.LTS(LX + "r1033tts.lxa")
M = morph.Morph(LEX, LT)


def our_lookup(word):
    r = LEX.lookup(word)
    if r:
        first = list(r[0][0])
        return first, [p for pr, p in r if list(pr) == first][:4], 0x1000
    ent, _stem = M.analyze(word)
    if ent:
        first = ent[0][0]
        return first, [p for pr, p, _lt in ent if pr == first][:4], ent[0][2]
    return LT.to_sapi(LT.pronounce(word)[0][1]), [0x1000], 0x2000


if __name__ == "__main__":
    L = lookup_log.lookups(sys.argv[1])
    ok = 0
    bad = []
    kinds = collections.Counter()
    for r in L:
        ours = our_lookup(r["text"])
        real = (r["pron"], r["posa"], r["lextype"])
        same = ours == real
        ok += same
        kinds[(hex(real[2]), same)] += 1
        if not same:
            bad.append((r["text"], r["stem"], real, ours))
    print(f"{ok}/{len(L)} exact;", dict(kinds))
    for b in bad[:30]:
        print(b)
