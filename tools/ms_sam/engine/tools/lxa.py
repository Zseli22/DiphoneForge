"""Reader for SAPI 5 compressed lexicon files (e.g. LTTS1033.LXA), as used by Microsoft Sam.

Format (reverse engineered from sapi.dll CCompressedLexicon, format GUID 12B545C3-3003-11D3-9C26-00C04F8EF87C):
  0x00 GUID format, 0x10 GUID lexicon, 0x20 u32 LCID, 0x24 u32 words, 0x28 u32 prons, 0x2c u32 ?,
  0x30 u32 hash slots, 0x34 u32 bits per slot, 0x38 u32 ?, 0x3c/0x40/0x44 u32 sizes of the letter,
  phone and POS Huffman codebooks, which follow at 0x48; then the hash table (slots * bits, MSB first),
  then the entry bit stream (u32 words, LSB first).
Codebook: u32 nsym, u32 nnodes, u32 root, u16 sym[nsym], (u16 zero, u16 one)[nnodes]; node < nsym is a
leaf when its first field is 0xFFFF.
Entry: word letters (letter book, 0-terminated), then repeated: 4-bit control (bits 0-2: 1 = phone string
follows (phone book, 0-terminated), 2 = part of speech follows (one POS-book symbol) closing the current
pronunciation; bit 3: last).
"""
import struct
import sys


class Codebook:
    def __init__(self, d, off):
        self.nsym, self.nnodes, self.root = struct.unpack_from("<3I", d, off)
        self.sym = struct.unpack_from(f"<{self.nsym}H", d, off + 12)
        self.nodes = [struct.unpack_from("<2H", d, off + 12 + 2 * self.nsym + 4 * k) for k in range(self.nnodes)]


class Lexicon:
    def __init__(self, path):
        d = open(path, "rb").read()
        self.d = d
        (self.lcid, self.n_words, self.n_prons, self.x2c, self.slots, self.slot_bits, self.x38,
         s1, s2, s3) = struct.unpack_from("<10I", d, 0x20)
        o = 0x48
        self.letters = Codebook(d, o)
        self.phones = Codebook(d, o + s1)
        self.pos = Codebook(d, o + s1 + s2)
        self.hash_off = o + s1 + s2 + s3
        self.hash_bytes = (self.slots * self.slot_bits + 7) >> 3
        self.data_off = self.hash_off + self.hash_bytes
        n = (len(d) - self.data_off) >> 2
        self.words = struct.unpack_from(f"<{n}I", d, self.data_off)
        self.empty = (1 << self.slot_bits) - 1

    def bit(self, pos):
        return (self.words[pos >> 5] >> (pos & 31)) & 1

    def decode(self, book, pos):
        node = book.root
        while book.nodes[node][0] != 0xFFFF:
            node = book.nodes[node][1] if self.bit(pos) else book.nodes[node][0]
            pos += 1
        return book.sym[node], pos

    def decode_string(self, book, pos):
        out = []
        while True:
            s, pos = self.decode(book, pos)
            if s == 0:
                return out, pos
            out.append(s)

    def slot(self, k):
        v = 0
        b = self.slot_bits * k
        for _ in range(self.slot_bits):
            v = (v << 1) | ((self.d[self.hash_off + (b >> 3)] >> (7 - (b & 7))) & 1)
            b += 1
        return v

    @staticmethod
    def hash(word, n):
        c = [ord(ch) for ch in word]
        h = c[0]
        prev = c[0]
        for ch in c[1:]:
            h = (h + ((prev << (ch & 31)) & 0xFFFFFFFF) + ((ch << (prev & 31)) & 0xFFFFFFFF)) & 0xFFFFFFFF
            prev = ch
        return ((h * 0xFFFF) & 0xFFFFFFFF) % n

    def entries_at(self, pos):
        """Decode the pronunciation entries that follow a word: [(phones, pos_tag)]."""
        out = []
        pending = None
        prev = []
        last = False
        while not last:
            ctl = 0
            for k in range(4):
                ctl |= self.bit(pos + k) << k
            pos += 4
            last = bool(ctl & 8)
            kind = ctl & 7
            if kind == 1:
                if pending is not None:
                    out.append((pending, 0xFFFFFFFF))
                pending, pos = self.decode_string(self.phones, pos)
                prev = pending
            elif kind == 2:
                p, pos = self.decode(self.pos, pos)
                out.append((pending if pending is not None else prev, p))
                pending = None
            else:
                raise ValueError(f"bad control {ctl} at {pos}")
        if pending is not None:
            out.append((pending, 0xFFFFFFFF))
        return out

    def lookup(self, word):
        w = word.lower()
        k = self.hash(w, self.slots)
        for _ in range(self.slots):
            off = self.slot(k)
            if off == self.empty:
                return None
            letters, pos = self.decode_string(self.letters, off)
            if "".join(map(chr, letters)).lower() == w:
                return self.entries_at(pos)
            k = (k + 1) % self.slots
        return None

    def all_words(self):
        seen = set()
        for k in range(self.slots):
            off = self.slot(k)
            if off == self.empty or off in seen:
                continue
            seen.add(off)
            letters, pos = self.decode_string(self.letters, off)
            yield "".join(map(chr, letters)), self.entries_at(pos)


if __name__ == "__main__":
    lex = Lexicon(sys.argv[1])
    print(f"lcid {lex.lcid:#x} words {lex.n_words} prons {lex.n_prons} slots {lex.slots} bits {lex.slot_bits}")
    for w in sys.argv[2:]:
        print(w, lex.lookup(w))
