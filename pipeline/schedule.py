"""Assign one puzzle per calendar date, matching each weekday's difficulty band.

The client never receives a solution -- it holds the whole graph, so it derives
hints and validity itself. A scheduled puzzle is just a (start, target) pair.

Difficulty ramps Mon -> Sun along two correlated axes:
  * chain scarcity: 40+ shortest chains on Monday down to 4-8 on Sunday
  * era: recent pairs early in the week, older pairs late

Those two are not independent. Recent players are densely connected (free
agency, larger rosters), so modern pairs are naturally short and route-rich,
while sparse multi-hop pairs only exist in the older eras. Weighting the hard
days toward the 80s/90s is therefore both a design choice and what the graph
will actually support.

Endpoint reuse is governed by --cooldown (days before a player may be an
endpoint again). --cooldown 0 means "never repeat an endpoint", which is the
strictest supply question and the one that caps total runway.
"""

import argparse
import datetime as dt
import json
import os
import random
from collections import Counter, defaultdict

import numpy as np

from graph import Universe, ROOT
from candidates import TIERS, ERAS, scan, with_eras, tiers_for

WEB = os.path.join(ROOT, "web", "data")

# Per-band era mix. Marginals over a week come out near 39% 2010s / 32% 2000s /
# 21% 1990s / 9% 1980s -- recent pairs common, 80s pairs a rare treat.
# Now that the data runs to 2026 there is a real 2020s bucket -- today's stars
# (Judge, Ohtani) have career midpoints there. Marginals come out near
# 30% 2020s / 25% 2010s / 20% 2000s / 15% 1990s / 10% 1980s: monotonically
# more frequent toward the present, with 80s pairs a rare hard treat.
ERA_BY_BAND = {
    "mon": {"2020s": 0.80, "2010s": 0.20},
    "tue": {"2020s": 0.65, "2010s": 0.35},
    "wed": {"2020s": 0.45, "2010s": 0.40, "2000s": 0.15},
    "thu": {"2010s": 0.40, "2000s": 0.40, "2020s": 0.20},
    "fri": {"2000s": 0.45, "1990s": 0.30, "2010s": 0.25},
    "sat": {"1990s": 0.45, "2000s": 0.25, "1980s": 0.30},
    "sun": {"1980s": 0.40, "1990s": 0.40, "2000s": 0.20},
}

BAND_BY_WEEKDAY = {wd: name for name, wd, *_ in TIERS}
BAND_CENTER = {name: (lo + min(hi, lo * 3)) / 2 for name, wd, lo, hi, _ in TIERS}
# Scarcest bands first: they have the fewest legal pairs, so they get first
# pick of the shared endpoint supply.
BAND_PRIORITY = ["sun", "sat", "fri", "thu", "mon", "tue", "wed"]
# Later in the week, prefer a genuine 3-link chain when one is available.
PREFER_LONG = {"thu", "fri", "sat", "sun"}


def era_sequence(band, n, rng):
    """A length-n era assignment for one band, matching its target mix."""
    weights = ERA_BY_BAND[band]
    counts = {e: int(n * w) for e, w in weights.items()}
    # hand out the rounding remainder to the heaviest eras first
    order = sorted(weights, key=lambda e: -weights[e])
    while sum(counts.values()) < n:
        counts[order[sum(counts.values()) % len(order)]] += 1
    seq = [e for e, k in counts.items() for _ in range(k)]
    rng.shuffle(seq)
    return seq


def build_schedule(u, rows, start_date, days, cooldown, seed=7,
                   avoid_ambiguous=True):
    dup_names = {n for n, k in Counter(u.names).items() if k > 1}
    rng = random.Random(seed)

    by_slot = defaultdict(list)     # (band, era) -> candidate pairs
    for s, t, d, c, fc, era in rows:
        if avoid_ambiguous and (u.names[s] in dup_names or u.names[t] in dup_names):
            continue
        for band in tiers_for(d, c):
            by_slot[(band, era)].append((s, t, d, c, fc, era))

    # Rank within a slot: chain count near the band's centre (most
    # representative of the intended feel), famous endpoints, and on the harder
    # days a nudge toward 3-link chains.
    for (band, era), lst in by_slot.items():
        center = BAND_CENTER[band]
        long_bonus = 0.35 if band in PREFER_LONG else 0.0
        lst.sort(key=lambda r: (
            abs(r[3] - center) / max(center, 1)
            - 0.004 * (u.fame[r[0]] + u.fame[r[1]])
            - (long_bonus if r[2] == 3 else 0.0)
        ))

    dates = [start_date + dt.timedelta(days=i) for i in range(days)]
    slots_by_band = defaultdict(list)
    for i, date in enumerate(dates):
        slots_by_band[BAND_BY_WEEKDAY[date.weekday()]].append(i)

    want_era = {}
    for band, idxs in slots_by_band.items():
        for day_i, era in zip(idxs, era_sequence(band, len(idxs), rng)):
            want_era[day_i] = era

    last_used = {}          # player idx -> day ordinal
    used_pairs = set()
    out = {}
    unfilled = []
    fallbacks = 0

    # Fill scarce bands first across the whole horizon, not date-by-date, so a
    # glut of Wednesdays can't strand the Sundays.
    slots = sorted(
        enumerate(dates),
        key=lambda kv: (BAND_PRIORITY.index(BAND_BY_WEEKDAY[kv[1].weekday()]), kv[0]),
    )

    def available(cand, day_i):
        s, t = cand[0], cand[1]
        if (s, t) in used_pairs:
            return False
        if cooldown == 0:
            return s not in last_used and t not in last_used
        return (day_i - last_used.get(s, -10**9) >= cooldown
                and day_i - last_used.get(t, -10**9) >= cooldown)

    for day_i, date in slots:
        band = BAND_BY_WEEKDAY[date.weekday()]
        target = want_era[day_i]
        # preferred era first, then the band's other eras by descending weight
        era_order = [target] + sorted(
            (e for e in ERA_BY_BAND[band] if e != target),
            key=lambda e: -ERA_BY_BAND[band][e],
        )
        pick = None
        for k, era in enumerate(era_order):
            for cand in by_slot.get((band, era), ()):
                if available(cand, day_i):
                    pick = cand
                    if k:
                        fallbacks += 1
                    break
            if pick:
                break
        if pick is None:
            unfilled.append(date)
            continue
        s, t, d, c, fc, era = pick
        if rng.random() < 0.5:      # which endpoint is start vs target
            s, t = t, s
        last_used[s] = last_used[t] = day_i
        used_pairs.add((pick[0], pick[1]))
        out[date.isoformat()] = {
            "n": day_i + 1, "s": s, "t": t, "d": d,
            "chains": int(c), "fchains": int(fc), "band": band, "era": era,
        }
    return out, unfilled, fallbacks


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pool", type=int, default=1200)
    ap.add_argument("--days", type=int, default=730)
    ap.add_argument("--cooldown", type=int, default=90,
                    help="days before an endpoint may repeat; 0 = never repeat")
    ap.add_argument("--start", default=None)
    ap.add_argument("--capacity", action="store_true",
                    help="probe runway under several repeat policies")
    args = ap.parse_args()

    u = Universe()
    rows = with_eras(u, scan(u, args.pool, verbose=False))
    start = (dt.date.fromisoformat(args.start) if args.start else dt.date.today())

    if args.capacity:
        print("pool %d, %d candidate pairs\n" % (args.pool, len(rows)))
        for cd in (0, 90, 180, 365):
            sched, unfilled, _ = build_schedule(u, rows, start, args.days, cd)
            label = "never repeat" if cd == 0 else "%d-day cooldown" % cd
            first_gap = min(unfilled).isoformat() if unfilled else "-"
            print("  %-16s filled %4d/%d days   first gap %s"
                  % (label, len(sched), args.days, first_gap))
        return

    sched, unfilled, fallbacks = build_schedule(
        u, rows, start, args.days, args.cooldown)
    path = os.path.join(WEB, "puzzles.json")
    with open(path, "w") as fh:
        json.dump(sched, fh, separators=(",", ":"), sort_keys=True)

    print("scheduled %d/%d days from %s (cooldown %d, %d era fallbacks)"
          % (len(sched), args.days, start, args.cooldown, fallbacks))
    if unfilled:
        print("UNFILLED: %d days, first %s" % (len(unfilled), min(unfilled)))
    print("%-14s %5.1f KB" % ("puzzles.json", os.path.getsize(path) / 1024))

    print("\n-- first two weeks --")
    for date in sorted(sched)[:14]:
        p = sched[date]
        wd = dt.date.fromisoformat(date).strftime("%a")
        print("  %s %s %-3s %-6s d=%d chains=%4d  %-22s -> %s"
              % (date, wd, p["band"], p["era"], p["d"], p["chains"],
                 u.names[p["s"]], u.names[p["t"]]))

    eras = Counter(p["era"] for p in sched.values())
    total = sum(eras.values())
    print("\nera mix: %s" % "  ".join(
        "%s %.0f%%" % (e, 100 * eras[e] / total) for e in ERAS))
    dists = Counter(p["d"] for p in sched.values())
    print("chain length: %s" % "  ".join(
        "d=%d %.0f%%" % (d, 100 * k / total) for d, k in sorted(dists.items())))
    ends = Counter()
    for p in sched.values():
        ends[p["s"]] += 1
        ends[p["t"]] += 1
    print("distinct endpoint players: %d   most reused: %s" % (
        len(ends), ", ".join("%s x%d" % (u.names[i], k)
                             for i, k in ends.most_common(3))))


if __name__ == "__main__":
    main()
