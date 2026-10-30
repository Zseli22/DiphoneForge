"""Word records -> segment records (the part of the engine between text analysis and the back end).

FUN_5ed4223d/FUN_5ed42301 (items), FUN_5ed5623e (position flags), FUN_5ed43dfb + voice method
+0x10 (unit selection), FUN_5ed44653/FUN_5ed4387b (durations), FUN_5ed541ad (F0),
FUN_5ed44696 (segment records).
"""
import struct

import numpy as np

import prosody_model as pm
import units_model as um

f32 = np.float32
SIL = 17
STRESS1, STRESS2, SYLBREAK = 43, 44, 46

# u32 phone properties @5ed31a50 (bit0 vowel, bit2 sonorant - boundary tone anchor)
PHONE_PROPS = [0x28003d, 0x20003d, 0x20003d, 0x20003d, 0x3d, 0x3d, 0x3d, 0x3d, 0x3d, 0x3d, 0x68003d, 0x40003d,
               0x40003d, 0x40003d, 0x40003d, 0x40003d, 0x20003d, 0x20, 0x20001b6, 0xc01b6, 0x20001b6, 0x1b6, 0x22,
               0x808976, 0x802976, 0x804976, 0x8008402, 0x8008406, 0x8010402, 0x8010406, 0x8002402, 0x8002406,
               0x8020402, 0x8020406, 0x809e02, 0x809e06, 0x803e02, 0x803e06, 0x805e02, 0x805e06, 0x1020e02,
               0x1020e06, 0xc06]
PAUSE_TABLE = [0.2, 0.2, 0.3, 0.3, 0.3, 0.3, 0.2, 0.2, 0.2, 0.2, 0.1, 0.01, 0.2, 0.001, 0.001, 0.001, 0.001, 0.001,
               0.001, 0.001, 0.001]
FINAL_LENGTHEN = [1.0, 1.33, 1.67, 1.67, 1.67, 1.67, 1.0, 1.0, 1.0, 1.0, 1.0, 1.3, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0,
                  1.0, 1.0]
RATE_TABLE = [3.0 ** (k / 10.0) for k in range(19)]
# internal phone id -> SAPI (American English) phone id, @5ed31b00
SAPI_PHONE = [28, 27, 21, 11, 10, 12, 13, 43, 15, 22, 23, 16, 36, 14, 35, 44, 0, 7, 46, 47, 38, 31, 26, 32, 33, 34,
              24, 45, 42, 20, 39, 48, 40, 49, 37, 17, 41, 19, 30, 25, 18, 29, 0, 8, 9, 0, 1]


def is_vowel(ph):
    return ph < len(PHONE_PROPS) and PHONE_PROPS[ph] & 1


def word_from_bytes(w, ctx_rate=None, next_btype=None):
    i = lambda o: struct.unpack_from("<i", w, o)[0]
    f = lambda o: struct.unpack_from("<f", w, o)[0]
    n = i(0x30)
    return dict(text=w[:0x28].decode("utf-16-le", "replace").split("\0")[0], phones=list(struct.unpack_from(f"<{n}i", w, 0x34)),
                ofs=i(0x63c), ln=i(0x640), x644=i(0x644), x648=i(0x648), vol=i(0x64c), x650=i(0x650), semi=i(0x654),
                emph=i(0x658), silence_ms=i(0x65c), acc=i(0x664), prom=i(0x668), bound=i(0x66c), bstr=i(0x670),
                btype=i(0x674), pause=f(0x678), rate=f(0x67c), x680=f(0x680), off=f(0x684), rng=f(0x688),
                rule_a=i(0x68c), rule_b=i(0x690), x694=i(0x694), pos=i(0x634), cls=i(0x638))


def new_item():
    """FUN_5ed41f74 defaults."""
    return dict(type=SIL, dur=0.0, base=0.0, flags=0, accent=0, prom=0, bound=0, bstr=0, vol=100, x0d4=0, semi=0,
                emph=0, silence_ms=0, pause_ms=0, word_ofs=0, word_len=0, x0f8=0, x0fc=0, btype=0, next_btype=0,
                bookmarks=0, rate=1.0, rate2=1.0, off=0.0, rng=1.0, x124=0, text=None)


def build_items(words):
    """FUN_5ed4223d (sentinels) + FUN_5ed44365/FUN_5ed42301 (one item per phone)."""
    start = new_item(); start["flags"] |= 1; start["x124"] = 0x12
    end = new_item(); end["flags"] |= 3; end["btype"] = 0x14; end["x124"] = 0x13
    items = []
    prev_ctx = None
    for wi, w in enumerate(words):
        nxt = words[wi + 1] if wi + 1 < len(words) else None
        P = w["phones"]
        n = len(P)
        accent_idx = anchor_idx = -1
        found = False
        for k, ph in enumerate(P):
            if ph < 0x2B:
                if not found and PHONE_PROPS[ph] & 1:
                    if k < n - 1 and P[k + 1] == STRESS1:
                        found = True
                        accent_idx = k
                    elif accent_idx < 0:
                        accent_idx = k
                if PHONE_PROPS[ph] & 4:
                    anchor_idx = k
        first = True
        for k, ph in enumerate(P):
            if ph >= 0x2B:
                continue
            if ph == SIL and w["btype"] > 12:
                if w["rate"] == 0.0:
                    w["rate"] = prev_ctx["rate"] if prev_ctx else 1.0
                continue
            it = new_item()
            it["type"] = ph
            it["word_ofs"], it["word_len"], it["x0f8"], it["x0fc"] = w["ofs"], w["ln"], w["x644"], w["x648"]
            if first:
                it["flags"] |= 1
                first = False
            if k < n - 1 and (P[k + 1] == STRESS1 or ph in (0xD, 0xB, 0xA, 0xC)):
                it["flags"] |= 0x40
            if ph == SIL:
                it["x124"] = w["x694"]
            if k == accent_idx:
                it["accent"] = w["acc"]
                it["text"] = w["text"]
            it["prom"] = w["prom"]
            if k == anchor_idx:
                it["bound"] = w["bound"]
            it["bstr"] = w["bstr"]
            it["vol"], it["x0d4"], it["semi"] = w["vol"], w["x650"], w["semi"]
            if w["emph"] > 0 and k == accent_idx:
                it["flags"] |= 0x40
                it["emph"] = w["emph"]
            it["silence_ms"] = w["silence_ms"]
            if w["rate"] == 0.0:
                it["rate"] = prev_ctx["rate"] if prev_ctx else 1.0
                w["rate"] = it["rate"]
            else:
                it["rate"] = w["rate"]
            it["rate2"] = w["x680"]
            it["next_btype"] = nxt["btype"] if nxt else 0
            it["off"], it["rng"] = w["off"], w["rng"]
            it["btype"] = w["btype"]
            if w["btype"] != 0:
                it["flags"] |= 3
            items.append(it)
        if w["pause"] > 0.0:
            it = new_item()
            it["word_ofs"], it["word_len"], it["x0f8"], it["x0fc"] = w["ofs"], w["ln"], w["x644"], w["x648"]
            it["vol"], it["x0d4"], it["semi"], it["emph"] = w["vol"], w["x650"], w["semi"], w["emph"]
            it["btype"], it["bstr"], it["prom"] = w["btype"], w["bstr"], w["prom"]
            it["base"] = w["pause"]
            it["pause_ms"] = int(np.trunc(np.float64(f32(w["pause"])) * 1000.0))
            it["rate2"], it["rate"] = 1.0, w["rate"]
            items.append(it)
        prev_ctx = w
    return [start] + items + [end]


def position_flags(items):
    """FUN_5ed5623e: bits 0x30 (syllable position) on vowels, 0x4/0xc on phones before word/phrase starts."""
    n = len(items)
    fl = [it["flags"] for it in items]
    ty = [it["type"] for it in items]
    for i in range(n):
        if is_vowel(ty[i]):  # FUN_5ed560f3
            local = 0
            j = i - 1
            while j > 0:
                if (fl[j] & 0xC) > 3:
                    break
                if is_vowel(ty[j]):
                    local = 0x30
                    break
                j -= 1
            j = i + 1
            while j < n:
                if fl[j] & 3:
                    fl[i] |= local
                    break
                if is_vowel(ty[j]):
                    if local == 0x30:
                        local = 0x20
                    elif local == 0:
                        local = 0x10
                j += 1
        j = i + 1  # FUN_5ed5609d
        while j < n:
            if fl[j] & 3:
                bits = (0xC if fl[j] & 2 else 0) | (4 if fl[j] & 1 else 0)
                fl[i] |= bits
            if is_vowel(ty[j]):
                break
            j += 1
    syllable_onsets(ty, fl)
    for it, f in zip(items, fl):
        it["flags"] = f


def legal_onset(c1, c2):
    """FUN_5ed55e62: may consonants c1 c2 start a syllable together?"""
    if c1 in (0x1A, 0x1B, 0x22, 0x23):
        return 0x14 <= c2 <= 0x15
    if c1 in (0x1C, 0x24, 0x25):
        return c2 in (0x12, 0x14)
    if c1 == 0x1E:
        if c2 in (0x12, 0x15, 0x17, 0x18, 0x1A, 0x22, 0x24, 0x26):
            return True
        return False
    if c1 == 0x20:
        return c2 == 0x12 or 0x14 <= c2 <= 0x15 or 0x17 <= c2 <= 0x18 or c2 in (0x22, 0x24)
    if c1 in (0x26, 0x27):
        return c2 == 0x12 or 0x14 <= c2 <= 0x15
    return False


def syllable_onsets(ty, fl):
    """FUN_5ed55f70: set 0x400 on the first phone of every syllable."""
    n = len(ty)
    mark = i = 0
    while i < n:
        while i < n and ty[i] == SIL:
            mark += 1
            i += 1
        if i >= n:
            break
        if not is_vowel(ty[i]):
            i += 1
            continue
        fl[mark] |= 0x400
        u = fl[i] & 0x30
        if u in (0, 0x30):
            j = i + 1
            while j < n and not (fl[j] & 3):
                j += 1
            mark = i = j
            continue
        cnt = -1
        j = i
        while True:
            last = j
            j += 1
            cnt += 1
            if is_vowel(ty[j]):
                break
        mark = i = j
        if cnt != 0:
            mark = i = last
            if cnt == 2:
                if legal_onset(ty[j - 2], ty[j - 1]):
                    mark = i = last - 1
            elif cnt == 3:
                if legal_onset(ty[j - 2], ty[j - 1]):
                    mark = i = (last - 1) if ty[j - 3] != 0x1E else (last - 2)
            elif cnt > 3:
                if not legal_onset(ty[j - cnt + 1], ty[j - cnt]):
                    mark = i = j - (cnt >> 1)
                else:
                    mark = i = j + (2 - cnt)


def select_units(items, selector, voice_phone_of):
    """FUN_5ed43dfb + voice method +0x10: fills unit, leaf, base duration, x5, amp."""
    phones, flags = [], []
    for it in items:
        phones.append(voice_phone_of(it["type"], 1 if it["flags"] & 0x40 else 0))
        flags.append(it["flags"] & 1)
    # sentence-start bit: first item that carries a word/sentence start of the original stream
    res = selector.select(phones, flags)
    for it, (unit, leaf, dur, x5, amp) in zip(items, res):
        it["unit"], it["leaf"], it["base"], it["x5"], it["amp"] = unit, leaf, f32(dur), f32(x5), f32(amp)
        it["vphone"] = None
    for it, ph in zip(items, phones):
        it["vphone"] = ph


def durations(items, rate_index=0):
    """FUN_5ed4387b with FUN_5ed43738 / FUN_5ed43790 / FUN_5ed437e3."""
    r = int(rate_index)
    sapi_rate = RATE_TABLE[min(r, 18)] if r >= 0 else 1.0 / RATE_TABLE[min(-r, 18)]
    sapi_rate = np.float64(f32(sapi_rate))
    first = items[0]
    first["base"] = f32(0.01)
    first["dur"] = f32(0.01)
    carry = 0
    for k in range(1, len(items)):
        it = items[k]
        base = np.float64(it["base"])
        add = 0.0
        div = np.float64(f32(np.float64(f32(it["rate2"])) * np.float64(f32(it["rate"])) * sapi_rate))
        vowel = is_vowel(it["type"])
        if it["type"] == SIL:
            if it["silence_ms"]:
                base = np.float64(f32(it["silence_ms"] * np.float64(f32(0.001))))
                div = 1.0
            elif it["pause_ms"]:
                base = np.float64(f32(it["pause_ms"] * np.float64(f32(0.001))))
            elif it["btype"]:
                b = np.float64(f32(PAUSE_TABLE[it["btype"]] if it["btype"] < len(PAUSE_TABLE) else 0.001))
                base = 1.0 if b > 1.0 else b
        else:
            fl = it["flags"] | (0x100 if it["emph"] > 0 else 0)  # FUN_5ed4387b: emphasis sets bit 8 locally
            if (fl & 8) and (fl & 0x1C0) and vowel:  # FUN_5ed43790
                base = np.float64(f32(np.float64(f32(FINAL_LENGTHEN[it["next_btype"]])) * base))
                if base > 5.0:
                    base = 5.0
                elif base < np.float64(f32(0.011)):
                    base = np.float64(f32(0.011))
            # FUN_5ed437e3 (only active with flags 0x100/0x200, set by emphasis markup)
            p = carry
            if not (fl & 0x200):
                if vowel:
                    p = 0
            if fl & 0x100:
                p = 1
            if p:
                if not vowel:
                    a = base * 1.25 - base
                    add += max(a, np.float64(f32(0.02)))
                else:
                    add += np.float64(f32(0.06))
            carry = p
        it["dur"] = f32((np.float64(f32(base)) + add) / div)


def to_prosody_items(items):
    return [dict(type=it["type"], dur=it["dur"], flags=it["flags"], accent=it["accent"], prom=it["prom"],
                 bound=it["bound"], bstr=it["bstr"], semi=it["semi"], off=it["off"], rng=it["rng"]) for it in items]


def segments(items, base_pitch=100.0, prange=0.4, stype=1, sr=22050.0):
    _, res = pm.build(to_prosody_items(items), base_pitch, prange, stype)
    segs = []
    for it, r in zip(items, res):
        segs.append(dict(unit=it["unit"], dur=float(it["dur"]), n=20,
                         t=np.array([f32(np.float64(t) * sr) for t in r["t"]], f32),
                         f0=np.array(r["f0"], f32), amp=np.full(20, it["amp"], f32)))
    return segs
