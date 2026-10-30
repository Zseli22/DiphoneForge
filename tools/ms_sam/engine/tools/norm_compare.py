"""Compare the C normalizer + tagger output (SAM_NODES=1) with the engine's node lists (samtap tag 16).

usage: python tools/norm_compare.py sentences.txt [-v]
Engine logs are cached in <file>_nodes/.
"""
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tag_log  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXE = os.path.join(ROOT, "build", "x64", "sam_say.exe")
TAP = os.path.join(ROOT, "harness", "samtap.exe")


def engine(text, cache, i):
    txt = os.path.join(cache, f"{i:04d}.txt")
    log = os.path.join(cache, f"{i:04d}.log")
    if not os.path.exists(log) or not os.path.exists(txt) or open(txt, encoding="utf-8").read() != text:
        open(txt, "w", encoding="utf-8").write(text)
        subprocess.run([TAP, "@" + txt, os.path.join(cache, "tmp.wav"), log, "2048"], capture_output=True)
    sents = []
    for s in tag_log.nodes(log):
        nodes = []
        for nd in s:
            words = []
            for w in nd["words"]:
                t = w["text"]
                if t.startswith("*") and t.endswith("*") and len(t) >= 2:
                    pron = [ord(c) for c in t[1:-1]]
                    t = w["text2"]
                else:
                    pron = w["pron"]
                if not t and not pron:
                    continue
                words.append((w["pos"], t, tuple(pron)))
            nodes.append((nd["type"], nd["text"], words))
        sents.append(nodes)
    return sents


def ours(text):
    env = dict(os.environ, SAM_NODES="1")
    tmp = os.path.join(ROOT, "ref", "tmp_text.txt")
    open(tmp, "w", encoding="utf-8").write(text)
    r = subprocess.run([EXE, "--data", os.path.join(ROOT, "data", "voice"), "@" + tmp, os.path.join(ROOT, "ref", "tmp.wav")],
                       capture_output=True, env=env)
    sents = []
    for line in r.stderr.decode("latin-1").splitlines():
        if line == "SENT":
            sents.append([])
        elif line.startswith("N "):
            ty, _, t = line[2:].partition(" ")
            sents[-1].append((int(ty, 16), t, []))
        elif line.startswith("E "):
            head, _, pr = line[2:].partition("\t")
            pos, _, t = head.partition(" ")
            sents[-1][-1][2].append((int(pos, 16), t, tuple(int(x) for x in pr.split())))
    return sents


def fmt(nodes):
    out = []
    for ty, t, words in nodes:
        ws = " ".join(f"{w[1]}/{w[0]:x}/{tag_log.phs(w[2]).replace(' ', '')}" for w in words)
        out.append(f"[{ty:x} {t!r}: {ws}]")
    return " ".join(out)


def same(a, b):
    if len(a) != len(b):
        return False
    for (ta, xa, wa), (tb, xb, wb) in zip(a, b):
        if ta != tb or xa != xb or len(wa) != len(wb):
            return False
        for (pa, sa, ra), (pb, sb, rb) in zip(wa, wb):
            if pa != pb or ra != rb:
                return False
    return True


def first_diff(a, b):
    for k in range(max(len(a), len(b))):
        if k >= len(a) or k >= len(b) or not same([a[k]], [b[k]]):
            return k
    return -1


def main():
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    src = sys.argv[1]
    verbose = "-v" in sys.argv
    lines = [l.rstrip(chr(10)) for l in open(src, encoding="utf-8") if l.strip()]
    cache = os.path.splitext(src)[0] + "_nodes"
    os.makedirs(cache, exist_ok=True)
    good = 0
    for i, text in enumerate(lines):
        e, o = engine(text, cache, i), ours(text)
        ok = len(e) == len(o) and all(same(a, b) for a, b in zip(e, o))
        good += ok
        if not ok or verbose:
            print(f"{i:4} {'OK ' if ok else 'BAD'} {text[:100]}")
            if not ok:
                for k in range(max(len(e), len(o))):
                    a = e[k] if k < len(e) else []
                    b = o[k] if k < len(o) else []
                    if not same(a, b):
                        d = first_diff(a, b)
                        print(f"     sentence {k}, node {d}:")
                        print(f"     engine: {fmt(a[max(0, d - 2):d + 4])}")
                        print(f"     ours  : {fmt(b[max(0, d - 2):d + 4])}")
                        break
        sys.stdout.flush()
    print(f"{good}/{len(lines)} lines match")


if __name__ == "__main__":
    main()
