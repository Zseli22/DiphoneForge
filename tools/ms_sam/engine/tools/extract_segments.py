"""Extract the raw segment records from a samtap log into a .seg file for samsynth."""
import sys

import taplog

log, out = sys.argv[1], sys.argv[2]
n = 0
with open(out, "wb") as f:
    for tag, b in taplog.records(log):
        if tag == taplog.T_SENT:
            f.write(b)
            n += 1
print(f"{n} segments -> {out}")
