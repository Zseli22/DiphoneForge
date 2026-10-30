"""Diff the C front end's word list (SAM_DEBUG) against the engine's pass-0 word list."""
import os
import struct
import subprocess
import sys

sys.path.insert(0, os.path.dirname(__file__))
import compare_corpus as cc  # noqa: E402
import frontend_model as fm  # noqa: E402
import taplog  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ours(text):
    env = dict(os.environ, SAM_DEBUG="1")
    r = subprocess.run([cc.EXE, "--data", os.path.join(ROOT, "data", "voice"), text, os.path.join(ROOT, "ref", "tmp.wav")],
                       capture_output=True, text=True, env=env)
    out = []
    for line in r.stderr.splitlines():
        if line.startswith("W "):
            p = line[2:].split("\t")
            out.append(dict(text=p[0], pos=int(p[1], 16), cls=int(p[2]), btype=int(p[3]), x694=int(p[4]),
                            phones=[int(x) for x in p[5].split()]))
    return out


def real(key):
    out = []
    for tag, b in taplog.records(os.path.join(cc.CORPUS, f"{key}.log")):
        if tag == 12 and struct.unpack_from("<i", b)[0] == 0:
            n = struct.unpack_from("<i", b, 4)[0]
            out += [fm.word_from_bytes(b[8 + j * 0x698: 8 + (j + 1) * 0x698]) for j in range(n)]
    return out


for key in sys.argv[1:]:
    o, r = ours(cc.SENTENCES[key]), real(key)
    print(f"== {key}: {cc.SENTENCES[key]}")
    for i in range(max(len(o), len(r))):
        a = o[i] if i < len(o) else None
        b = r[i] if i < len(r) else None
        sa = f"{a['text'][:12]:12} {a['pos']:#6x} bt{a['btype']} {' '.join(map(str, a['phones']))}" if a else "-"
        sb = f"{b['text'][:12]!r:14} {b['pos'] & 0xffff:#6x} bt{b['btype']} {' '.join(map(str, b['phones']))}" if b else "-"
        same = a and b and a["phones"] == b["phones"] and a["pos"] == (b["pos"] & 0xFFFFFFFF) and a["btype"] == b["btype"]
        print(f"   {sa:58} | {sb}{'' if same else '   <<<'}")
