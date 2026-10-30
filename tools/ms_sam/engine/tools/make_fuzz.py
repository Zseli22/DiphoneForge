"""Generate random but plausible-looking text lines that exercise the normalizer.

usage: python tools/make_fuzz.py N seed out.txt
"""
import random
import sys

WORDS = ["the", "a", "cat", "Dr.", "St.", "Mr.", "Mrs.", "etc.", "e.g.", "U.S.A.", "NASA", "IBM", "read", "live", "wind",
         "polish", "Polish", "May", "Jan.", "Monday", "Tues.", "sec", "min", "ft", "kg", "cm", "in", "oz", "lb", "mph",
         "am", "pm", "a.m.", "p.m.", "WA", "NY", "CA", "OK", "us", "US", "as", "AS", "hello", "world", "dollars",
         "cents", "million", "billion", "percent", "o'clock", "don't", "it's", "we'll", "rock'n'roll", "e-mail",
         "well-known", "x-ray", "-ing", "AT&T", "C", "F", "K", "fig", "p", "No.", "vs.", "Co.", "Inc.", "Ave.", "Blvd."]
PUNCT = [",", ".", "!", "?", ";", ":", "-", "--", "...", "\"", "'", "(", ")", "[", "]", "{", "}", "/", "&", "#", "%",
         "*", "+", "=", "@", "$", "’", "“", "”", "—", "–", "©", "™", "½", "°",
         "€", "£", "é", "ü", "ß"]


def number(r):
    k = r.randrange(16)
    if k == 0:
        return str(r.randrange(10))
    if k == 1:
        return str(r.randrange(10000))
    if k == 2:
        return "{:,}".format(r.randrange(10 ** r.randrange(4, 13)))
    if k == 3:
        return "%d.%d" % (r.randrange(1000), r.randrange(100))
    if k == 4:
        return "%d/%d" % (r.randrange(1, 20), r.randrange(1, 20))
    if k == 5:
        n = r.randrange(1, 200)
        return str(n) + r.choice(["st", "nd", "rd", "th"])
    if k == 6:
        return "$" + r.choice(["5", "1,250.50", "0.99", "2", "10.05", "3.5"])
    if k == 7:
        return "%d:%02d" % (r.randrange(0, 30), r.randrange(0, 70))
    if k == 8:
        return "%d/%d/%d" % (r.randrange(1, 32), r.randrange(1, 32), r.randrange(0, 3000))
    if k == 9:
        return "%03d-%04d" % (r.randrange(1000), r.randrange(10000))
    if k == 10:
        return "(%03d) %03d-%04d" % (r.randrange(1000), r.randrange(1000), r.randrange(10000))
    if k == 11:
        return "%d%%" % r.randrange(200)
    if k == 12:
        return "%ds" % (r.randrange(10) * 10 + 1900)
    if k == 13:
        return "%d-%d" % (r.randrange(100), r.randrange(100))
    if k == 14:
        return "-" + str(r.randrange(100))
    return "%d:%02d:%02d" % (r.randrange(24), r.randrange(60), r.randrange(60))


def token(r):
    k = r.random()
    if k < 0.45:
        return r.choice(WORDS)
    if k < 0.75:
        return number(r)
    if k < 0.85:
        return r.choice(WORDS) + r.choice(PUNCT)
    if k < 0.93:
        return r.choice(PUNCT)
    return "".join(r.choice("abcXYZ019$%-./:'") for _ in range(r.randrange(1, 8)))


def main():
    n, seed, out = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
    r = random.Random(seed)
    lines = []
    for _ in range(n):
        toks = [token(r) for _ in range(r.randrange(1, 14))]
        lines.append(" ".join(toks))
    open(out, "w", encoding="utf-8").write("\n".join(lines) + "\n")


if __name__ == "__main__":
    main()
