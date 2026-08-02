"""Sweep the generation cap: how much chain-length variety does it cost?"""
import json, os, sys
from collections import Counter
import datetime as dt
from graph import Universe, ROOT
import candidates as C
from schedule import build_schedule

CACHE = os.path.join(ROOT, "data", "scan_cache.json")

u = Universe()
if os.path.exists(CACHE):
    raw = [tuple(r) for r in json.load(open(CACHE))]
else:
    raw = C.scan(u, 1200, verbose=False)
    json.dump([list(r) for r in raw], open(CACHE, "w"))
print("raw candidate pairs (pre-era): %d\n" % len(raw))

start = dt.date(2026, 8, 1)
print("%-8s %8s %6s %s" % ("spread", "pairs", "d=3%", "scheduled era mix / fill"))
for spread in (12, 16, 20, 25, 35, 999):
    rows = C.with_eras(u, raw, spread=spread)
    d3 = sum(1 for r in rows if r[2] == 3)
    sched, unfilled, fb = build_schedule(u, rows, start, 730, 90)
    eras = Counter(p["era"] for p in sched.values())
    tot = max(sum(eras.values()), 1)
    mix = " ".join("%s:%2.0f%%" % (e[:4], 100*eras[e]/tot) for e in C.ERAS)
    sd3 = 100 * sum(1 for p in sched.values() if p["d"] == 3) / tot
    print("%-8s %8d %5.1f%%  %s  fill %d/730 d3:%2.0f%%"
          % (spread, len(rows), 100*d3/max(len(rows),1), mix, len(sched), sd3))
