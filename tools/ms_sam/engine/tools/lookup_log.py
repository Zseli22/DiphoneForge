"""Parse samtap tag-13 records (word struct after FUN_5ed4b1f9)."""
import struct

import taplog


def parse_lookup(b):
    hr = struct.unpack_from("<i", b)[0]
    w = b[4:]
    text = w[:0x100].decode("utf-16-le", "replace").split("\0")[0]
    stem = w[0x100:0x200].decode("utf-16-le", "replace").split("\0")[0]
    lextype = struct.unpack_from("<I", w, 0x200)[0]
    plen = struct.unpack_from("<I", w, 0x204)[0]
    pron = list(struct.unpack_from(f"<{min(plen, 0x17f)}H", w, 0x208))
    na = struct.unpack_from("<I", w, 0x508)[0]
    posa = list(struct.unpack_from(f"<{min(na, 4)}I", w, 0x50C))
    alen = struct.unpack_from("<I", w, 0x51C)[0]
    alt = list(struct.unpack_from(f"<{min(alen, 0x17f)}H", w, 0x520))
    nb = struct.unpack_from("<I", w, 0x820)[0]
    posb = list(struct.unpack_from(f"<{min(nb, 4)}I", w, 0x824))
    chosen = struct.unpack_from("<I", w, 0x834)[0]
    return dict(hr=hr, text=text, stem=stem, lextype=lextype, pron=pron, posa=posa, alt=alt, posb=posb, chosen=chosen,
                has_alt=w[0x83C], use_alt=w[0x840])


def lookups(path):
    return [parse_lookup(b) for tag, b in taplog.records(path) if tag == 13]
