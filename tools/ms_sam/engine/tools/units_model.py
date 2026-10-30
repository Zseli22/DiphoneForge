"""Phone -> unit selection (voice method FUN_5ed58bfa + tree walker FUN_5ed58816).

Section 0 = phone set, section 1 = per-leaf statistics, section 2 = decision trees.
"""
import struct

POS_CODE = {"b": 1, "e": 2, "s": 4, "": 8}


class UnitSelector:
    def __init__(self, data, sections):
        self.d = data
        self.s0 = sections[0][0]
        self.s1 = sections[1][0]
        self.s2 = sections[2][0]
        d, s0 = data, self.s0
        self.n_phones = struct.unpack_from("<i", d, s0 + 0x24)[0]
        name_tab = s0 + struct.unpack_from("<i", d, s0 + 0x1C)[0]
        self.names = []
        for i in range(self.n_phones):
            o = s0 + struct.unpack_from("<i", d, name_tab + 4 * i)[0]
            self.names.append(d[o:d.index(b"\0", o)].decode())
        self.phone_id = {n: i for i, n in enumerate(self.names)}
        self.sil = self.phone_id["SIL"]
        self.unit_base_sub = struct.unpack_from("<i", d, s0 + 9 * 4)[0]  # voice +0x4c

    def i32(self, o):
        return struct.unpack_from("<i", self.d, self.s2 + o)[0]

    def leaf(self, phone, left, right, pos):
        """FUN_5ed58816: walk the decision tree of `phone` for the given context."""
        d, s2 = self.d, self.s2
        root = s2 + 0x134 + phone * 8
        if struct.unpack_from("<h", d, root)[0] == 0:
            raise ValueError(f"no tree for phone {phone}")
        posbits = POS_CODE[pos]
        if self._is_sil(left):
            left = self.i32(0x28)
        if self._is_sil(right):
            right = self.i32(0x28)
        elif self.i32(0x2C) >= 0 and posbits in (2, 4):
            right = self.i32(0x2C)
        lb = self.i32(0x1C) + 4 + left
        rb = self.i32(0x1C) + 4 + right
        half_words = self.i32(4)
        q_base = s2 + self.i32(0x338)
        q_words = self.i32(0x33C)
        nodes = s2 + struct.unpack_from("<i", d, root + 4)[0]
        node = nodes
        while True:
            yes = struct.unpack_from("<h", d, node + 4)[0]
            if yes < 0:
                return struct.unpack_from("<h", d, node + 6)[0]
            qlist = s2 + struct.unpack_from("<i", d, node)[0]
            nxt = struct.unpack_from("<h", d, node + 6)[0]
            while True:
                q = struct.unpack_from("<H", d, qlist)[0]
                if q == 0xFFFF:
                    break
                qb = q_base + q * q_words * 4
                w = lambda k: struct.unpack_from("<I", d, qb + 4 * k)[0]
                if (w(0) & posbits) == posbits and (w(lb // 32) >> (lb % 32)) & 1 and \
                        (w(half_words + rb // 32) >> (rb % 32)) & 1:
                    nxt = yes
                    break
                qlist += 2
            node = nodes + nxt * 10

    def _is_sil(self, p):
        n = self.names[p]
        return n.startswith("+") or n.upper().startswith("SIL")

    def unit_id(self, phone, leaf):
        return self.i32(0x34 + phone * 4) - self.unit_base_sub + 1 + leaf

    def leaf_stats(self, phone, leaf):
        """(duration_s, x5, amplitude) as stored into the phone record."""
        d = self.d
        tab = self.s1 + struct.unpack_from("<i", d, self.s1 + phone * 4)[0]
        f = struct.unpack_from("<4f", d, tab + leaf * 16)
        import numpy as np
        f32 = np.float32
        return (float(f32(np.float64(f[0]) * np.float64(f32(0.001)))), f[2], float(f32(np.sqrt(np.float64(f[3])))))

    def select(self, phones, flags):
        """phones: list of phone ids; flags: bit0 = word start. Returns [(unit, leaf, dur, x5, amp)]."""
        out = []
        n = len(phones)
        for i, ph in enumerate(phones):
            if ph == self.sil:
                out.append((0, 0, 0.01, 0.0, 1.0))
                continue
            left = phones[i - 1] if i > 0 else self.sil
            if i < n - 1:
                right, nflags = phones[i + 1], flags[i + 1]
            else:
                right, nflags = self.sil, 0
            if flags[i] & 1:
                pos = "s" if nflags & 1 else "b"
            else:
                pos = "e" if nflags & 1 else ""
            lf = self.leaf(ph, left, right, pos)
            dur, x5, amp = self.leaf_stats(ph, lf)
            out.append((self.unit_id(ph, lf), lf, dur, x5, amp))
        return out
