"""Word-level prosody passes run on the word list before the item builder.

pass0: FUN_5ed454df (+ FUN_5ed4471c): exclamation emphasis, pauses before emphasized words
pass1: FUN_5ed45651 (+ FUN_5ed43d88): POS-driven phrase breaks, question type
pass2: FUN_5ed43f71: pitch accents, prominence (MSVC rand()), boundary tones
Words are dicts as produced by frontend_model.word_from_bytes.
"""
SIL = 0x11


class MsvcRand:
    """MSVC CRT rand(): per-thread LCG, default seed 1."""

    def __init__(self, seed=1):
        self.state = seed & 0xFFFFFFFF

    def __call__(self):
        self.state = (self.state * 214013 + 2531011) & 0xFFFFFFFF
        return (self.state >> 16) & 0x7FFF


def new_word():
    """FUN_5ed41ea0 defaults (only the fields the passes use)."""
    return dict(text="", phones=[], ofs=0, ln=0, x644=0, x648=0, vol=100, x650=0, semi=0, emph=0, silence_ms=0,
                acc=0, prom=0, bound=0, bstr=0, btype=0, pause=0.0, rate=0.0, x680=1.0, off=0.0, rng=1.0,
                rule_a=0, rule_b=0, x694=0, pos=0, cls=0)


def is_sil(w):
    return len(w["phones"]) > 0 and w["phones"][0] == SIL


def pass0(words):
    # FUN_5ed4471c: in sentences ending with '!' (btype 2) pick one word to emphasize
    if words and words[-1]["btype"] == 2:
        n_content = n_all = 0
        pick = None
        prio = [None] * 5  # 0x5000, 0x3002, 0x2000, 0x3001, 0x1000
        order = {0x5000: 0, 0x3002: 1, 0x2000: 2, 0x3001: 3, 0x1000: 4}
        for w in words:
            if w["cls"] == 2:
                n_content += 1
                n_all += 1
                if n_content == 1:
                    pick = w
                k = order.get(w["pos"])
                if k is not None and prio[k] is None:
                    prio[k] = w
            elif w["cls"] == 1:
                n_all += 1
                if n_all == 1:
                    pick = w
        if not (n_content != 1 and n_all != 1):
            if pick is not None:
                pick["emph"] = 1
        elif n_content >= 2:
            for p in prio:
                if p is not None:
                    pick = p
                    break
            pick["emph"] = 1
    # FUN_5ed454df: insert a 1 ms pause before an emphasized word not preceded by punctuation
    out = []
    prev_btype = 1
    prev_out = None
    for idx, w in enumerate(words):
        if idx < len(words) - 1 and w["emph"] > 0 and prev_btype == 0:
            x = new_word()
            x.update(silence_ms=1, phones=[SIL], btype=0, ofs=w["ofs"], ln=w["ln"], x694=6)
            if prev_out is None:
                x["x680"] = 1.0
            else:
                x["x680"] = prev_out["x680"]
                x["vol"] = prev_out["vol"]
            out.append(x)
            prev_out = x
        else:
            prev_out = w
        out.append(w)
        prev_btype = w["btype"]
    return out


def _question_type(w, cq):
    """FUN_5ed43d88: '?' words become rising (3) unless the phrase started with a wh-word (4)."""
    if w["btype"] in (3, 4):
        if cq:
            w["btype"], w["rule_b"] = 3, 0xE
        else:
            w["btype"], w["rule_b"] = 4, 0xF


def pass1(words):
    """FUN_5ed45651: phrase breaks from part-of-speech patterns."""
    words = list(words)
    n = len(words)
    if n <= 0:
        return words
    prev_btype = 1
    since = 0
    b_conj = b_det = b_next = False
    cq = False
    prev_pos = 0
    prev_word = None
    i = 0
    while i < n - 1:
        W = words[i]
        N = words[i + 1] if i + 1 < n else None
        N2 = words[i + 2] if i + 2 < n else None
        if 1 <= prev_btype <= 12:
            since = 1
            cq = True
            b_conj = False
            b_det = False
        else:
            since += 1
        pos = W["pos"]
        t = W["text"]
        wh = len(t) >= 2 and t[0] in "Ww" and t[1] in "Hh"
        npos = N["pos"] if N else 0
        n2pos = N2["pos"] if N2 else 0
        if since == 1:
            if pos == 0x4005 or wh:
                cq = False
            elif pos in (0x4009, 0x4003, 0x4004):
                b_conj = True
        elif since == 2 and b_conj and (pos in (0x4005, 0x1004) or wh):
            cq = False
        brk = None
        if b_next:
            brk = (0xD, 1)
            b_next = False
        else:
            check = False
            if since == 1 and pos == 0x3002 and npos == 0x4006:
                b_next = True
                check = True
            else:
                b_next = False
                if pos == 0x4004:
                    if b_det or since < 4 or n2pos == 0x4003:
                        check = True
                    else:
                        brk = (0xE, 2)
                elif pos != 0x3002 or since < 5 or npos == 0x3001:
                    check = True
                else:
                    brk = (0xE, 3)
            if check:
                if prev_pos == 0x1003 and since > 2:
                    brk = (0xE, 4)
                elif pos in (0x1002, 0x4007) and since > 3 and prev_pos not in (0x1004, 0x4003):
                    brk = (0xE, 5)
                elif pos == 0x4005 and since > 4:
                    brk = (0xE, 6)
                elif since > 2 and prev_pos in (0x1000, 0x2000) and pos == 0x4001:
                    brk = (0xF, 7)
                else:
                    go = False
                    if prev_pos == 0x1000:
                        if npos in (0x1004, 0x4003, 0x4004) or since < 4 or pos != 0x2000:
                            go = True
                        else:
                            brk = (0xF, 9)
                    elif prev_pos in (0x2000, 0x3001, 0x3002):
                        go = True
                    if go and pos not in (0x1000, 0x2000, 0x3001, 0x3002) and N is not None and N["btype"] == 0:
                        brk = (0x12, 0xD)
        cur = W
        if brk is not None and i > 0 and prev_btype == 0 and W["btype"] == 0:
            btype, rule = brk
            x = new_word()
            x.update(btype=btype, phones=[SIL], ofs=W["ofs"], ln=W["ln"], text="+", x694=7)
            if prev_word is not None:
                prev_word["rule_a"] = rule
                prev_word["rule_b"] = rule
                prev_word["acc"] = 5
                x["x680"] = prev_word["x680"]
                x["vol"] = prev_word["vol"]
            else:
                x["x680"] = 1.0
            words.insert(i, x)
            n += 1
            i += 1
            cur = x
        _question_type(cur, cq)
        prev_word = cur
        prev_btype = cur["btype"]
        if since > 2:
            b_det = False
        if pos == 0x4006:
            b_det = True
        prev_pos = pos
        i += 1
    if words:
        _question_type(words[-1], cq)
    return words


def pass2(words, rand):
    """FUN_5ed43f71: accents, prominences and boundary tones."""
    n = len(words)
    if n == 0:
        return words
    i = 0
    while i < n - 1 and words[i]["phones"][:1] == [SIL]:
        i += 1
    first = words[i]
    q_first = first["cls"] == 3
    # runs of equal word class (silences do not start a run)
    runs = []
    cur_cls = words[0]["cls"]
    start = i
    has_emph = False
    for k in range(i, n):
        w = words[k]
        if w["cls"] != cur_cls and w["phones"][:1] != [SIL]:
            runs.append((cur_cls, start, k - 1))
            start = k
            cur_cls = w["cls"]
        if w["emph"] > 0:
            has_emph = True
    runs.append((cur_cls, start, n - 1))
    for cls, a, b in runs:
        if cls in (1, 3) and b != a:
            w = words[b]
            if w["acc"] == 0:
                w["acc"], w["prom"], w["rule_a"] = 2, 2, 0xF
        elif cls in (2, 0):
            w = words[a]
            if w["acc"] == 0:
                w["acc"] = 1
                w["rule_a"] = 0x10
                w["prom"] = rand() % 5
    for k in range(n - 1):
        w, nx = words[k], words[k + 1]
        bt = nx["btype"]
        if bt == 0:
            continue
        if bt == 1:
            if w["cls"] == 2:
                w["acc"], w["prom"] = 5, 10
                if w["rule_a"] == 0:
                    w["rule_a"] = 0x13
            w["bound"], w["bstr"] = 0x3EB, 5
            if w["rule_b"] == 0:
                w["rule_b"] = 0x11
        elif bt == 2 or 3 < bt < 6:
            if w["cls"] == 2:
                w["acc"], w["prom"] = 1, 4
                if w["rule_a"] == 0:
                    w["rule_a"] = 0x12
            w["bound"], w["bstr"] = 0x3EA, 10
            if w["rule_b"] == 0:
                w["rule_b"] = 0x10
        elif bt == 3:
            w["acc"], w["prom"], w["bound"], w["bstr"] = 2, 10, 0x3EC, 10
            if w["rule_a"] == 0:
                w["rule_a"] = 0x11
            if w["rule_b"] == 0:
                w["rule_b"] = 0xE
            if q_first:
                first["acc"], first["prom"], first["rule_a"] = 1, 5, 0xE
        elif bt == 0x13:
            w["bound"], w["bstr"] = 0x3EB, 5
            if w["rule_b"] == 0:
                w["rule_b"] = 0x12
        else:
            if w["cls"] == 2:
                w["acc"], w["prom"] = 5, 10
                if w["rule_a"] == 0:
                    w["rule_a"] = nx["rule_a"]
            w["bound"], w["bstr"] = 0x3EB, 5
            if w["rule_b"] == 0:
                w["rule_b"] = nx["rule_b"]
    if has_emph:
        prev = None
        for w in words:
            if w["emph"] < 1:
                if w["acc"] != 0 and w["prom"] > 5:
                    w["prom"] = 5
            else:
                w["acc"], w["prom"], w["bound"] = 7, 10, 0
                if prev is not None:
                    prev["bound"] = 0
            prev = w
    return words
