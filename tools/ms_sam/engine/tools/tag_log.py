"""Read tagger records (tags 14/15/16) from a samtap log."""
import struct
import sys

sys.path.insert(0, __file__.rsplit("\\", 1)[0].rsplit("/", 1)[0])

PH = ("- ! & , . ? _ 1 2 aa ae ah ao aw ax ay b ch d dh eh er ey f g h ih iy jh k l m n ng ow oy p r s sh t th uh "
      "uw v w y z zh").split()


def phs(ids):
    return " ".join(PH[i - 1] if 1 <= i <= len(PH) else f"#{i}" for i in ids)


def records(path):
    data = open(path, "rb").read()
    o = 0
    while o + 8 <= len(data):
        tag, n = struct.unpack_from("<II", data, o)
        yield tag, data[o + 8:o + 8 + n]
        o += 8 + n


def wstr(b, o, n):
    return b[o:o + 2 * n].decode("utf-16le", "replace")


def entry(b):
    text = b[0:0x100].decode("utf-16le", "replace").split("\0")[0]
    na = struct.unpack_from("<I", b, 0x508)[0]
    posa = list(struct.unpack_from("<4I", b, 0x50c))[:na]
    nb = struct.unpack_from("<I", b, 0x820)[0]
    posb = list(struct.unpack_from("<4I", b, 0x824))[:nb]
    pron0 = list(struct.unpack_from("<%dH" % 0x180, b, 0x208))
    pron1 = list(struct.unpack_from("<%dH" % 0x180, b, 0x520))
    pron0 = pron0[:pron0.index(0)]
    pron1 = pron1[:pron1.index(0)]
    pos, lock = struct.unpack_from("<II", b, 0x834)
    altok = b[0x83c]
    idx, star = struct.unpack_from("<II", b, 0x840)
    return dict(text=text, posa=posa, posb=posb, pron0=pron0, pron1=pron1, pos=pos, lock=lock, altok=altok,
                idx=idx, star=star, lextype=struct.unpack_from("<I", b, 0x200)[0])


def tag_sets(path):
    """[(entries_in, entries_out)] per tagger call."""
    out, pend = [], None
    for tag, b in records(path):
        if tag in (14, 15):
            n = struct.unpack_from("<I", b, 0)[0]
            es = [entry(b[4 + k * 0x848:4 + (k + 1) * 0x848]) for k in range(n)]
            if tag == 14:
                pend = es
            else:
                out.append((pend, es))
    return out


def nodes(path):
    res = []
    for tag, b in records(path):
        if tag != 16:
            continue
        nn = struct.unpack_from("<I", b, 0)[0]
        o = 4
        sent = []
        for _ in range(nn):
            ty, nw, d2, d5, ln = struct.unpack_from("<IIIII", b, o)
            o += 20
            text = wstr(b, o, ln)
            o += 2 * ln
            words = []
            for _ in range(nw):
                wt, w20, w24, pos = struct.unpack_from("<iiiI", b, o)
                o += 16
                strs = []
                for _k in range(3):
                    n = struct.unpack_from("<I", b, o)[0]
                    o += 4
                    strs.append(b[o:o + 2 * n])
                    o += 2 * n
                pron = list(struct.unpack("<%dH" % (len(strs[2]) // 2), strs[2]))
                words.append(dict(type=wt, w20=w20, w24=w24, pos=pos, text=strs[0].decode("utf-16le"),
                                  text2=strs[1].decode("utf-16le"), pron=pron))
            sent.append(dict(type=ty, text=text, words=words, d2=d2, d5=d5))
        res.append(sent)
    return res


if __name__ == "__main__":
    for ins, outs in tag_sets(sys.argv[1]):
        print("--- tagger")
        for a, b in zip(ins, outs):
            print(f"  {a['text']:12} posa={[hex(x) for x in a['posa']]} posb={[hex(x) for x in a['posb']]} "
                  f"alt={a['altok']} lock={a['lock']:x} {a['pos']:x}->{b['pos']:x} idx {b['idx']} star {b['star']} "
                  f"| {phs(a['pron0'])} | {phs(a['pron1'])}")
    for sent in nodes(sys.argv[1]):
        print("--- nodes")
        for nd in sent:
            print(f"  node type {nd['type']:x} d2 {nd['d2']:x} d5 {nd['d5']:x} {nd['text']!r}")
            for w in nd["words"]:
                pr = w['pron']
                t2 = w['text2']
                print(f"     w type {w['type']} +20 {w['w20']} +24 {w['w24']:x} pos {w['pos']:x} {w['text']!r} {t2!r} {pr if pr and pr[0] == 42 else phs(pr)}")
