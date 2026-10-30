"""Letter-to-sound for Microsoft Sam (r1033tts.lxa, spcommon.dll "LTS Lexicon").

File: 0x24-byte header (GUID, GUID, u16 LCID at 0x20), a text phone table ("NAME hexids\\n"...) ending
in NUL, then the binary model (FUN_5bd46df3):
  letter symbols:  u32 n, u32 off[n], u32 size, char blob[size]
  output symbols:  u32 m, u32 off[m], u32 size, char blob[size]
  letter questions: u32 nq, u32 words, u32 bits[nq][words]
  output questions: u32 nq, u32 words, u32 bits[nq][words]
  for letter k = 1..n-1: u32 nodes, (u16 skip, u16 ?, u32 expr_off)[nodes], u32 dsize, dist blob,
                         u32 esize, expr blob
Prediction (FUN_5bd47056 / FUN_5bd46675 / FUN_5bd469b0): per letter a decision tree over neighbouring
letters and already chosen outputs gives counts per output symbol; combinations whose counts exceed
10% of the total are expanded, probabilities multiplied, then normalised, sorted and the top 10 with
p >= 0.01 returned.
"""
import struct

import numpy as np

f32 = np.float32


def msvc_qsort(a, comp):
    """MSVC CRT qsort (quicksort + shortsort for <= 8 elements); reproduces its order for ties."""
    def swap(i, j):
        if i != j:
            a[i], a[j] = a[j], a[i]

    def shortsort(lo, hi):
        while hi > lo:
            mx = lo
            for p in range(lo + 1, hi + 1):
                if comp(a[p], a[mx]) > 0:
                    mx = p
            swap(mx, hi)
            hi -= 1

    n = len(a)
    if n < 2:
        return
    stack = []
    lo, hi = 0, n - 1
    while True:
        size = hi - lo + 1
        if size <= 8:
            shortsort(lo, hi)
        else:
            mid = lo + size // 2
            swap(mid, lo)
            loguy, higuy = lo, hi + 1
            while True:
                loguy += 1
                while loguy <= hi and comp(a[loguy], a[lo]) <= 0:
                    loguy += 1
                higuy -= 1
                while higuy > lo and comp(a[higuy], a[lo]) >= 0:
                    higuy -= 1
                if higuy < loguy:
                    break
                swap(loguy, higuy)
            swap(lo, higuy)
            if higuy - 1 - lo >= hi - loguy:
                if lo + 1 < higuy:
                    stack.append((lo, higuy - 1))
                if loguy < hi:
                    lo = loguy
                    continue
            else:
                if loguy < hi:
                    stack.append((loguy, hi))
                if lo + 1 < higuy:
                    hi = higuy - 1
                    continue
        if not stack:
            return
        lo, hi = stack.pop()


class LTS:
    def __init__(self, path):
        d = open(path, "rb").read()
        self.d = d
        self.lcid = struct.unpack_from("<H", d, 0x20)[0]
        end = d.index(b"\0", 0x24)
        self.phone_map = {}
        for line in d[0x24:end].decode("ascii").split("\n"):
            if line.strip():
                name, hexids = line.split()
                self.phone_map[name] = [int(hexids[k:k + 4], 16) for k in range(0, len(hexids), 4)]
        p = end + 1
        i32 = lambda o: struct.unpack_from("<i", d, o)[0]

        def strtab(p):
            n = i32(p)
            offs = struct.unpack_from(f"<{n}i", d, p + 4)
            size = i32(p + 4 + 4 * n)
            base = p + 8 + 4 * n
            names = [d[base + o:d.index(b"\0", base + o)].decode("latin-1") for o in offs]
            return names, base + size

        self.letters, p = strtab(p)
        self.outputs, p = strtab(p)
        self.qsets = []
        for _ in range(2):
            nq, words = i32(p), i32(p + 4)
            p += 8
            qs = []
            for _q in range(nq):
                qs.append(struct.unpack_from(f"<{words}I", d, p))
                p += 4 * words
            self.qsets.append(qs)
        self.trees = [None]
        for _k in range(1, len(self.letters)):
            n = i32(p)
            nodes_off = p + 4
            p = nodes_off + 8 * n
            dsize = i32(p)
            dist = p + 4
            p = dist + dsize
            esize = i32(p)
            p += 4
            expr = p if esize > 0 else None
            p += max(esize, 0)
            self.trees.append((nodes_off, dist, expr))
        self.letter_index = {name.lower(): k for k, name in enumerate(self.letters)}

    # ---- decision trees -------------------------------------------------------------------------
    def _term(self, code, ctx_letters, ctx_out):
        t = 1 if code > 20000 else 0
        if t:
            code -= 20000
        kind = 3 if code > 10000 else 2
        if kind == 3:
            code -= 10000
        off, q = divmod(code, 1000)
        buf, pos = (ctx_letters if t == 0 else ctx_out)
        for _ in range(off):
            if buf[pos] == 0:
                break
            pos += -1 if kind == 2 else 1
        v = buf[pos]
        bits = self.qsets[t][q]
        return (bits[v >> 5] >> (v & 31)) & 1

    def _expr(self, p, ctx_letters, ctx_out):
        """FUN_5bd463a6: OR of AND-terms (0xfffe separates, 0xffff ends). Returns (value, next_clause)."""
        d = self.d
        while True:
            code = struct.unpack_from("<H", d, p)[0]
            neg = 1 if code & 0x8000 else 0
            code &= 0x7FFF
            if self._term(code, ctx_letters, ctx_out) ^ neg:
                p += 2
                nxt = struct.unpack_from("<H", d, p)[0]
                if nxt == 0xFFFF:
                    return 1, None
                if nxt == 0xFFFE:
                    return 1, p + 2
                continue
            while True:
                w = struct.unpack_from("<H", d, p)[0]
                if w == 0xFFFE:
                    return 0, p + 2
                if w == 0xFFFF:
                    return 0, None
                p += 2

    def distribution(self, letter_sym, ctx_letters, ctx_out):
        nodes_off, dist, expr = self.trees[letter_sym]
        d = self.d
        node = nodes_off
        while True:
            skip = struct.unpack_from("<H", d, node)[0]
            if skip == 0:
                break
            e = expr + struct.unpack_from("<I", d, node + 4)[0]
            val = 0
            while e is not None:
                val, e = self._expr(e, ctx_letters, ctx_out)
                if val == 1:
                    break
            node = node + skip * 8 if val == 1 else node + skip * 8 + 8
        o = dist + struct.unpack_from("<I", d, node + 4)[0]
        n = struct.unpack_from("<i", d, o)[0]
        return [struct.unpack_from("<hh", d, o + 4 + 4 * k) for k in range(n)]

    # ---- search ----------------------------------------------------------------------------------
    def _expand(self, letters, i, outs, thr):
        """FUN_5bd46675: list of (prob, [output syms]) for letters[i:] (letters has 0 sentinels)"""
        ctx_letters = (letters, i)
        cur = outs + [1]                          # current output slot marked 1
        ctx_out = (cur + [0], len(cur) - 1)
        dist = self.distribution(letters[i], ctx_letters, ctx_out)
        total = sum(c for _s, c in dist)
        ftot = f32(total)
        res = []
        last = i == len(letters) - 2
        for sym, cnt in dist:
            if not (np.float64(ftot) * np.float64(f32(thr)) < np.float64(cnt)):
                continue
            if last:
                res.append((f32(np.float64(cnt) * (1.0 / np.float64(ftot))), [sym & 0xFF]))
            else:
                child = self._expand(letters, i + 1, outs + [sym & 0xFF], thr)
                inv = np.float64(f32(1.0 / np.float64(ftot)))
                for pr, s in child:
                    res.append((f32(np.float64(cnt) * np.float64(pr) * inv), [sym & 0xFF] + s))
        return res

    LETTER_NAMES = ['EY', 'B IY', 'S IY', 'D IY', 'IY', 'EH F', 'JH IY', 'EY CH', 'AY', 'JH EY', 'K EY', 'EH L',
                    'EH M', 'EH N', 'OW', 'P IY', 'K Y UW', 'AA R', 'EH S', 'T IY', 'Y UW', 'V IY',
                    'D AH B AX L Y UW', 'EH K S', 'W AY', 'Z IY', 'EY Z', 'B IY Z', 'S IY Z', 'D IY Z', 'IY Z',
                    'EH F S', 'JH IY Z', 'EY CH AX Z', 'AY Z', 'JH EY Z', 'K EY Z', 'EH L Z', 'EH M Z', 'EH N Z',
                    'OW Z', 'P IY Z', 'K Y UW Z', 'AA R Z', 'EH S AX Z', 'T IY Z', 'Y UW Z', 'V IY Z',
                    'D AH B AX L Y UW Z', 'EH K S AX Z', 'W AY Z', 'Z IY Z']
    BOGUS = "B OW G AH S P R AH N AH N S IY EY SH AH N"

    def spell(self, word):
        """FUN_5bd46cf9: letter names, 's handled as plural names."""
        out = []
        w = word
        i = 0
        if w and w[0] in "'":  # FUN_5bd48c45 test on the first char
            i = 1
        while i < len(w):
            c = w[i]
            nxt = i + 1
            if c.isascii() and c.isalpha():
                lc = c.lower()
                k = ord(lc) - 97
                if nxt < len(w) and w[nxt] == "'":
                    after = w[nxt + 1] if nxt + 1 < len(w) else ""
                    if (after == "" and lc == "s") or after in ("s", "S"):
                        k += 26
                        nxt += 1 if after else 0
                out.append(self.LETTER_NAMES[k])
            i = nxt
        return " ".join(out)

    def pronounce(self, word):
        """FUN_5bd47056: full LTS lexicon behaviour. Returns [(prob, phone names)]."""
        w = word.strip().split()[0] if word.strip() else ""
        if not w:
            return [(1.0, self.BOGUS)]
        has_vowel = False
        all_upper = True
        syms = []
        for c in w:
            lc = c.lower()
            if not has_vowel and lc in "aeiouy":
                has_vowel = True
            if all_upper and c == lc:
                all_upper = False
            k = self.letter_index.get(lc, -1)
            if k > 0:
                syms.append((k, lc))
        out = []
        n = len(syms)
        if 0 < n < 0x80:
            if n == 1:
                name = self.letters[syms[0][0]].lower()
                if "a" <= name <= "z":
                    out = [(1.0, self.LETTER_NAMES[ord(name) - 97])]
            elif has_vowel:
                if all_upper:
                    out = [(1.0, self.spell(w))]
                out += self.predict(w, nbest=10 - len(out))
            else:
                out = [(1.0, self.spell(w))]
        if not out:
            out = [(1.0, self.BOGUS)]
        return out

    def predict(self, word, nbest=10):
        """Returns [(prob, phone name string)] like FUN_5bd47056 (without the spelling paths)."""
        syms = []
        for ch in word.lower():
            k = self.letter_index.get(ch, -1)
            if k > 0:
                syms.append(k)
        letters = [0] + syms + [0]
        if not syms:
            return []
        res = self._expand(letters, 1, [0], 0.1)
        if not res:
            return []
        tot = 0.0
        for pr, _ in res:
            tot += np.float64(pr)
        res = [(f32((1.0 / tot) * np.float64(pr)), o) for pr, o in res]
        msvc_qsort(res, lambda a, b: -1 if a[0] > b[0] else (1 if a[0] < b[0] else 0))  # FUN_5bd46978
        res = res[:nbest]
        out = []
        for pr, o in res:
            if pr < f32(0.01):
                continue
            names = []
            for sym in o:
                if sym < len(self.outputs):
                    names.append(self.outputs[sym])
            txt = " ".join(names)
            clean = []
            k = 0
            while k < len(txt):
                c = txt[k]
                if c == "#":
                    k += 2
                    continue
                clean.append(" " if c == "_" else c)
                k += 1
            txt = " ".join("".join(clean).split())
            if txt:  # empty results are not counted (FUN_5bd469b0)
                out.append((float(pr), txt))
        return out

    def to_sapi(self, phone_names):
        ids = []
        for name in phone_names.split():
            ids += self.phone_map.get(name, [])
        return ids
