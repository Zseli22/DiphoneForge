"""Morphology (suffix stripping) of Microsoft Sam's normalizer: FUN_5ed4ee14 + FUN_5ed4e449/4eba0/4e07b.

A word not in the lexicon is stripped of known suffixes (right to left); the stem is looked up with
spelling repairs (add e, i->y, undouble consonant, ...). The pronunciation is rebuilt from the stem's
with the suffix phones (s/z/ax z and t/d/ax d chosen by the stem's last phone), and the part of
speech is mapped through each suffix's POS table. If no stem is found but suffixes were removed,
the stem goes through letter-to-sound instead. Pronunciations are SAPI phone id lists.
"""
SAPI_NAMES = ['-', '!', '&', ',', '.', '?', '_', '1', '2', 'aa', 'ae', 'ah', 'ao', 'aw', 'ax', 'ay', 'b', 'ch', 'd',
              'dh', 'eh', 'er', 'ey', 'f', 'g', 'h', 'ih', 'iy', 'jh', 'k', 'l', 'm', 'n', 'ng', 'ow', 'oy', 'p', 'r',
              's', 'sh', 't', 'th', 'uh', 'uw', 'v', 'w', 'y', 'z', 'zh']
NAME_ID = {n: i + 1 for i, n in enumerate(SAPI_NAMES)}


def ids(names):
    return [NAME_ID[n.lower()] for n in names.split()]


# (reversed suffix letters, record index) @5ed36110
SUFFIXES = [("RE", 5), ("TSE", 6), ("GNI", 2), ("ELBA", 14), ("ELBI", 14), ("YLDE", 12), ("YLBA", 24), ("YLBI", 24),
            ("YLLACI", 26), ("YLI", 28), ("YL", 13), ("Y", 11), ("TNEM", 8), ("RO", 7), ("SSEN", 15), ("SSEL", 10),
            ("EZICI", 30), ("EZI", 17), ("ZI", 18), ("MSICI", 29), ("MSI", 16), ("DE", 1), ("S'", 3), ("S", 0),
            ("'", 4), ("EGA", 9), ("DOOH", 19), ("LUF", 20), ("EKIL", 21), ("ESIW", 22), ("HSI", 23), ("PIHS", 25),
            ("EMOS", 27)]
# records @5edb13f8: (phones, [(from_pos, to_pos)], flags)
RECORDS = [
    (" s", [(0x2000, 0x2000), (0x1000, 0x1000)], 0x0),
    (" d", [(0x2000, 0x2000), (0x2000, 0x3001)], 0x7),
    (" IH NG", [(0x2000, 0x2000), (0x2000, 0x3001), (0x2000, 0x1000)], 0x5),
    (" s", [(0x1000, 0x1000)], 0x0),
    (" s", [(0x1000, 0x1000)], 0x0),
    (" ER", [(0x2000, 0x1000), (0x3001, 0x3001), (0x3002, 0x3002), (0x3001, 0x3002)], 0x7),
    (" AX s t", [(0x3001, 0x3001), (0x3002, 0x3002), (0x3001, 0x3002)], 0x7),
    (" ER", [(0x2000, 0x1000)], 0x5),
    (" m AX n t", [(0x2000, 0x1000)], 0x2),
    (" IH JH", [(0x2000, 0x1000)], 0x5),
    (" l IH s", [(0x1000, 0x3001)], 0x2),
    (" IY", [(0x1000, 0x3001), (0x3001, 0x3002)], 0x5),
    (" AX d l IY", [(0x2000, 0x3001), (0x2000, 0x3002)], 0x7),
    (" l IY", [(0x1000, 0x3001), (0x3001, 0x3002)], 0x10),
    (" AX - b AX l", [(0x2000, 0x3001), (0x1000, 0x3001)], 0x7),
    (" n IH s", [(0x3001, 0x1000)], 0x2),
    (" IH z AX m", [(0x3001, 0x1000), (0x1000, 0x1000)], 0x1),
    (" AY z", [(0x1000, 0x2000), (0x3001, 0x2000)], 0x1),
    (" AY z", [(0x1000, 0x2000), (0x3001, 0x2000)], 0x1),
    (" h UH d", [(0x1000, 0x1000)], 0x0),
    (" f AX l", [(0x1000, 0x3001), (0x2000, 0x3001)], 0x0),
    (" l AY k", [(0x1000, 0x3001)], 0x0),
    (" w AY z", [(0x1000, 0x3001)], 0x2),
    (" IH SH", [(0x1000, 0x3001)], 0x5),
    (" AX - b l IY", [(0x2000, 0x3002), (0x1000, 0x3002)], 0x7),
    (" SH IH 2 p", [(0x1000, 0x1000)], 0x0),
    (" L IY", [(0x3001, 0x3002)], 0x0),
    (" S AX M", [(0x1000, 0x3001)], 0x2),
    (" AX L IY", [(0x1000, 0x3002)], 0xC),
    (" IH z AX m", [(0x3001, 0x1000), (0x1000, 0x1000)], 0x1),
    (" AY z", [(0x1000, 0x2000), (0x3001, 0x2000)], 0x1),
]
REC_PH = [ids(r[0]) for r in RECORDS]
S, Z, AXZ = ids("s"), ids("z"), ids("AX z")
T, D, AXD = ids("t"), ids("d"), ids("AX d")
AXL, IY, L = ids("AX l"), ids("IY"), ids("l")
# SAPI phone class flags @5ed36428
PHONE_CLASS = [1, 1, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 2, 2, 2, 3, 5, 3, 3, 2, 2, 2, 1, 3, 1, 2, 2, 7, 1, 3, 3, 3,
               3, 2, 2, 1, 3, 1, 5, 1, 1, 2, 2, 3, 3, 3, 3, 7]
NOT_FOUND = "not found"
LEX_VENDOR, LEX_LTS, DERIVED = 0x1000, 0x2000, 0x4000


def match_suffix(w, n):
    """FUN_5ed4d88c: reversed-letter match at the end of w[:n]; returns (record, stem_len) or (-1, n)."""
    for rev, rec in SUFFIXES:
        i = n - 1
        if w[i] != rev[0]:
            continue
        k = 0
        found = -1
        while True:
            if i < 2:
                break
            if found != -1:
                return rec, i + 1
            i -= 1
            k += 1
            if k == len(rev):
                found = rec
            if found == -1 and w[i] != rev[k]:
                break
            if found != -1:
                continue
        if found != -1:
            return found, i + 1
    return -1, n


def plural(pron):
    last = pron[-1]
    cls = PHONE_CLASS[last] if last < 0x32 else 0
    if (cls & 4) or last == S[0] or last == Z[0]:
        return pron + AXZ
    if (cls & 1) == 0 or (cls & 2):
        return pron + Z
    return pron + S


def past(pron):
    last = pron[-1]
    if last in (T[0], D[0]):
        return pron + AXD
    if (PHONE_CLASS[last] & 2) == 0:
        return pron + T
    return pron + D


class Morph:
    def __init__(self, lexicon, lts):
        self.lex = lexicon
        self.lts = lts

    def lookup(self, letters):
        """FUN_5ed4d909 with types user|app|vendor: [(pron ids, pos)] or None"""
        r = self.lex.lookup("".join(letters))
        if not r:
            return None
        return [(list(p), pos) for p, pos in r]

    def lts_lookup(self, text):
        return [(self.lts.to_sapi(s), 0xFFFFFFFF) for _p, s in self.lts.pronounce(text)]

    def analyze(self, word):
        """Returns (entries [(pron, pos, lextype)], stem) or (None, None)."""
        W = list(word.upper())
        n = len(W)
        stack = []          # records, innermost suffix last-pushed (list head)
        prons = None
        found = False
        lts_used = False
        cut = n
        stem_len = n
        keep = True
        result = NOT_FOUND
        while keep:
            k, L2 = match_suffix(W, cut)
            if k == -1:
                keep = False
                if stack:
                    prons = self.lts_lookup(word[:cut])
                    if prons:
                        lts_used = True
                        found = True
                        stem_len = cut
                break
            stack.insert(0, k)
            sl = L2
            rec = RECORDS[k]
            ok = None
            if k == 0:
                if W[sl - 1] == "S":
                    stack.pop(0)
                    keep = False
                    if stack:
                        prons = self.lts_lookup(word[:sl + 1])
                        if prons:
                            lts_used = True
                            found = True
                            stem_len = sl + 1
                    break
                ok = self.lookup(W[:sl])
                if ok:
                    prons, stem_len, found = ok, sl, True
                    break
                if W[sl - 1] != "E":
                    cut = sl
                    continue
                if W[sl - 2] == "I":  # FUN_5ed4dca2: -ies -> -y
                    W2 = W[:sl - 2] + ["Y"]
                    ok = self.lookup(W2)
                    if ok:
                        W[sl - 2] = "Y"
                        prons, stem_len, found = ok, sl - 1, True
                        break
                ok = self.lookup(W[:sl - 1])
                if ok:
                    prons, stem_len, found = ok, sl - 1, True
                    break
                cut = sl
                continue
            if k == 0x18:  # -ably: try the -able word, then turn its "ax l" into "l" and add -y
                W2 = W[:sl + 3] + ["E"]
                ok = self.lookup(W2)
                if ok:
                    W[sl + 3] = "E"
                    for p, _pos in ok:
                        if len(p) > 2 and p[-2:] == AXL:
                            p[-2:] = L
                    stack[0] = 11
                    prons, stem_len, found = ok, sl + 4, True
                    break
            elif k == 0x1A:  # -ically: keep "ic"
                ok = self.lookup(W[:sl + 2])
                if ok:
                    prons, stem_len, found = ok, sl + 2, True
                    break
                cut = sl + 2
                continue
            elif k == 0x1C:  # -ily: word ending in y
                W2 = W[:sl] + ["Y"]
                ok = self.lookup(W2)
                if ok:
                    W[sl] = "Y" if sl < len(W) else W.append("Y")
                    for p, _pos in ok:
                        if p and p[-1] == IY[0]:
                            p.pop()
                    prons, stem_len, found = ok, sl + 1, True
                    break
                cut = sl
                continue
            elif k in (0x1D, 0x1E):  # -icism / -icize: keep "ic", last phone becomes s
                ok = self.lookup(W[:sl + 2])
                if ok:
                    for p, _pos in ok:
                        if p:
                            p[-1] = S[0]
                    prons, stem_len, found = ok, sl + 2, True
                    break
                cut = sl + 2
                continue
            # general path (LAB_5ed4f075)
            last = W[sl - 1]
            if (rec[2] & 1) and last not in ("O", "W", "Y") and (last != "E" or k == 1):
                W2 = W[:sl] + ["E"]
                ok = self.lookup(W2)
                if ok:
                    if sl > 0 and W[sl - 1] == "L":
                        for p, _pos in ok:
                            if len(p) > 1 and p[-2:] == AXL:
                                p[-2:] = L
                    prons, stem_len, found = ok, sl + 1, True
                    break
            ok = self.lookup(W[:sl])
            if ok:
                prons, stem_len, found = ok, sl, True
                break
            if rec[2] & 2 and W[sl - 1] == "I":
                ok = self.lookup(W[:sl - 1] + ["Y"])
                if ok:
                    prons, stem_len, found = ok, sl, True
                    break
            if rec[2] & 4 and W[sl - 1] not in "AEFHIKOSUWYZ" and W[sl - 1] == W[sl - 2]:
                ok = self.lookup(W[:sl - 1])
                if ok:
                    prons, stem_len, found = ok, sl - 1, True
                    break
            if rec[2] & 0x10:
                ok = self.lookup(W[:sl] + ["L"])
                if ok:
                    for p, _pos in ok:
                        if p and p[-1] == L[0]:
                            p.pop()
                    prons, stem_len, found = ok, sl + 1, True
                    break
            cut = sl
        if not found or not stack or not prons:
            return None, None
        stem = "".join(W[:stem_len])
        if lts_used:
            return self.combine_lts(stack, prons), stem
        return self.combine(stack, prons, LEX_VENDOR), stem

    def apply(self, pron, k):
        ph = REC_PH[k]
        if ph == S:
            return plural(pron)
        if ph == D:
            return past(pron)
        pron = list(pron)
        if k in (29, 30) and pron:
            pron[-1] = S[0]
        return pron + ph

    def combine(self, stack, prons, lextype):
        """FUN_5ed4e449"""
        out = []
        seen_pos = []
        for pron, pos in prons:
            cur = [pos]
            ok_all = True
            for k in stack:
                nxt = []
                applied = False
                new_pron = pron
                for p in cur:
                    for frm, to in RECORDS[k][1]:
                        if frm == p and to not in nxt:
                            nxt.append(to)
                            if len(nxt) == 1 and not applied:
                                new_pron = self.apply(pron, k)
                                applied = True
                if not nxt:
                    ok_all = False
                    break
                pron, cur = new_pron, nxt
            if ok_all:
                for p in cur:
                    if p not in seen_pos:
                        seen_pos.append(p)
                        out.append((pron, p, lextype | DERIVED))
        if not out:  # FUN_5ed4e07b fallback
            pron = list(prons[0][0])
            for k in stack:
                pron = pron + REC_PH[k]
            last = stack[-1]
            out = [(pron, to, lextype | DERIVED) for _frm, to in RECORDS[last][1]]
        return out

    def combine_lts(self, stack, prons):
        """FUN_5ed4eba0"""
        k = stack[0]
        pron = self.apply(prons[0][0], k)
        pos = []
        for _frm, to in RECORDS[k][1]:
            if to not in pos:
                pos.append(to)
        entries = [(pron, p) for p in pos]
        if len(stack) > 1:
            return self.combine(stack[1:], entries, LEX_LTS)
        return [(pron, p, LEX_LTS | DERIVED) for pron, p in entries]
