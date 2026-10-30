"""Read data from spttseng.dll by virtual address (no Ghidra needed).

usage: python tools/pe_read.py            (library use: from pe_read import Pe)
"""
import os
import struct

DLL = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "extracted", "WinXP_TTS_Voices",
                   "$COMMONFILES", "SpeechEngines", "Microsoft", "TTS", "1033", "spttseng.dll")


class Pe:
    def __init__(self, path=DLL):
        self.d = open(path, "rb").read()
        pe = struct.unpack_from("<I", self.d, 0x3c)[0]
        nsec = struct.unpack_from("<H", self.d, pe + 6)[0]
        optsz = struct.unpack_from("<H", self.d, pe + 20)[0]
        self.base = struct.unpack_from("<I", self.d, pe + 24 + 28)[0]
        self.secs = []
        o = pe + 24 + optsz
        for i in range(nsec):
            name, vsize, va, rsize, raw = struct.unpack_from("<8sIIII", self.d, o + 40 * i)
            self.secs.append((va, max(vsize, rsize), raw, rsize))

    def off(self, va):
        rva = va - self.base
        for sva, size, raw, rsize in self.secs:
            if sva <= rva < sva + size:
                if rva - sva >= rsize:
                    return None
                return raw + rva - sva
        return None

    def u32(self, va):
        return struct.unpack_from("<I", self.d, self.off(va))[0]

    def i32(self, va):
        return struct.unpack_from("<i", self.d, self.off(va))[0]

    def u16(self, va):
        return struct.unpack_from("<H", self.d, self.off(va))[0]

    def wstr(self, va, n=200):
        o = self.off(va)
        if o is None:
            return None
        s = []
        for i in range(n):
            c = struct.unpack_from("<H", self.d, o + 2 * i)[0]
            if c == 0:
                break
            s.append(chr(c))
        return "".join(s)


if __name__ == "__main__":
    pe = Pe()
    print(hex(pe.base), pe.wstr(0x5ed37678))
