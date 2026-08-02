"""Scan famous-player pairs and tier them into difficulty bands.

Also answers the supply question: with *no endpoint ever reused*, how many
puzzles can we field? That is a maximum matching on the candidate-pair graph,
solved exactly with Blossom rather than estimated greedily.
"""

import argparse
import json
import os
from collections import Counter

import numpy as np

from graph import Universe, ROOT

# Difficulty bands, keyed by shortest-chain count (see spec 6, reinterpreted:
# "valid chains" == number of distinct *shortest* chains).
#
# Chain count is the primary knob and distance is left free within 2..3, because
# the graph will not support anything else: modern players are so densely
# connected that distance-3 pairs essentially do not exist after ~2005 (the
# 2010s yield 108 of them, against 26,555 for the 1990s). Pinning the hard days
# to distance 3 would have made them unschedulable in the recent eras.
# name, weekday, min_chains, max_chains, allowed distances
TIERS = [
    ("mon", 0, 40, 10**9, (2, 3)),
    ("tue", 1, 25, 40, (2, 3)),
    ("wed", 2, 18, 28, (2, 3)),
    ("thu", 3, 12, 20, (2, 3)),
    ("fri", 4, 8, 14, (2, 3)),
    ("sat", 5, 6, 10, (2, 3)),
    ("sun", 6, 4, 8, (2, 3)),
]

MIN_CHAINS = 4        # fairness floor: never a single forced path
MAX_DIST = 3          # spec 6: minimum chain length never exceeds 3

# Era mix. A pair's era is the decade containing the mean of the two endpoints'
# career midpoints -- roughly when a fan would have been watching them.
ERA_MIN_YEAR = 1980   # pairs centred before this are excluded entirely
ERAS = ["1980s", "1990s", "2000s", "2010s", "2020s"]

# Endpoints must rank in the top N *of their own era*, not of all history.
# A global cut is unfair to active players: fame accrues over a whole career,
# so a current star is always mid-table against retired Hall of Famers no
# matter how famous they actually are. Ranking within era removes that bias.
# Smaller caps for older decades: an 80s puzzle should be Ozzie Smith, not a
# name only a historian recalls.
ERA_POOL = {
    "1980s": 250,
    "1990s": 300,
    "2000s": 350,
    "2010s": 400,
    "2020s": 400,
}


def player_era(y0, y1, i):
    """Decade bucket for a single player, by career midpoint."""
    mid = (y0[i] + y1[i]) / 2
    decade = int(mid // 10 * 10)
    return "%ds" % decade


def era_rank(u):
    """Rank of each player within their own era, by fame (0 = most famous)."""
    import collections
    by_era = collections.defaultdict(list)
    for i in range(u.n):
        by_era[player_era(u.y0, u.y1, i)].append(i)
    rank = np.full(u.n, 10**9, dtype=np.int64)
    for era, members in by_era.items():
        members.sort(key=lambda i: -u.fame[i])
        for r, i in enumerate(members):
            rank[i] = r
    return rank


# Both endpoints must be of the same baseball generation. Without this, the
# midpoint average happily labels Chipper Jones + Mickey Mantle as "1980s" --
# a decade in which neither of them played.
MAX_MID_SPREAD = 12     # years between the two career midpoints
MIN_CAREER_OVERLAP = 0  # seasons the two careers must share (0 = adjacent ok)


def pair_era(y0, y1, s, t):
    """Decade bucket for a pair, or None if it is out of window or cross-era."""
    mid_s = (y0[s] + y1[s]) / 2
    mid_t = (y0[t] + y1[t]) / 2
    if abs(mid_s - mid_t) > MAX_MID_SPREAD:
        return None
    overlap = min(y1[s], y1[t]) - max(y0[s], y0[t])
    if overlap < MIN_CAREER_OVERLAP:
        return None
    mid = (mid_s + mid_t) / 2
    if mid < ERA_MIN_YEAR:
        return None
    decade = int(mid // 10 * 10)
    return "%ds" % decade


def tiers_for(dist, chains):
    """Every band this pair legally satisfies (bands overlap by design)."""
    out = []
    for name, wd, lo, hi, dists in TIERS:
        if dist in dists and lo <= chains <= hi:
            out.append(name)
    return out


CONNECTOR_POOL = 2000   # players a knowledgeable fan could plausibly name
MIN_FAMOUS_CHAINS = 2   # spec 6: a fan must be able to solve it unaided
MIN_ENDPOINT_YEAR = 0   # endpoints must still be active in/after this season


def scan(u, pool_size, max_dist=MAX_DIST, verbose=True,
         connector_pool=CONNECTOR_POOL, min_famous=MIN_FAMOUS_CHAINS,
         min_year=MIN_ENDPOINT_YEAR):
    """Candidate pairs as (s, t, dist, chains, famous_chains), pre-era.

    `chains` counts every shortest chain (any real player is a legal guess).
    `famous_chains` counts only those routed entirely through recognizable
    connectors -- that is the one a fan can actually find unaided.
    """
    order = np.argsort(-u.fame)
    pool = [int(i) for i in order[:pool_size] if u.y1[i] >= min_year]
    rank = np.empty(u.n, dtype=np.int64)
    rank[order] = np.arange(u.n)

    # Connector-eligible subgraph, built once. Endpoints are drawn from the
    # (smaller) fame pool so they are always inside it.
    conn_mask = np.zeros(u.n, dtype=bool)
    conn_mask[order[:connector_pool]] = True
    conn_mask[pool] = True
    A_sub, remap, keep = u.subgraph(conn_mask)
    n_sub = len(keep)

    rows = []
    for s in pool:
        dist, count = u.walk_profile(s, max_depth=max_dist)
        fdist, fcount = u.walk_profile_on(A_sub, remap[s], n_sub, max_depth=max_dist)
        for t in pool:
            if t <= s:
                continue
            d = int(dist[t])
            if d < 2 or d > max_dist:
                continue
            c = float(count[t])
            if c < MIN_CHAINS:
                continue
            # a famous-connector chain must exist *at the same length*
            fc = float(fcount[remap[t]]) if int(fdist[remap[t]]) == d else 0.0
            if fc < min_famous:
                continue
            rows.append((s, t, d, c, fc))

    if verbose:
        print("pool %d  candidate pairs %d" % (pool_size, len(rows)))
        by_d = Counter(r[2] for r in rows)
        for d in sorted(by_d):
            print("  distance %d: %6d pairs" % (d, by_d[d]))
    return rows


def with_eras(u, rows, pool_by_era=None, spread=None, rank=None):
    """Attach an era to each pair, dropping cross-era and under-famous ones."""
    global MAX_MID_SPREAD
    pool_by_era = pool_by_era or ERA_POOL
    if spread is not None:
        MAX_MID_SPREAD = spread
    if rank is None:
        rank = era_rank(u)
    out = []
    for s, t, d, c, fc in rows:
        era = pair_era(u.y0, u.y1, s, t)
        if era is None or era not in pool_by_era:
            continue
        # Each endpoint must rank inside the top N of *its own* era. A player
        # can straddle: someone centred in 1979 may still anchor an 80s pair,
        # so eras outside the table fall back to the pair's own cap.
        cap_s = pool_by_era.get(player_era(u.y0, u.y1, s), pool_by_era[era])
        cap_t = pool_by_era.get(player_era(u.y0, u.y1, t), pool_by_era[era])
        if rank[s] >= cap_s or rank[t] >= cap_t:
            continue
        out.append((s, t, d, c, fc, era))
    return out


def tiered(rows):
    buckets = {name: [] for name, *_ in TIERS}
    for s, t, d, c, fc, era in rows:
        for name in tiers_for(d, c):
            buckets[name].append((s, t, d, c, fc, era))
    return buckets


def max_matching(rows, weight_by=None):
    """Largest set of pairs sharing no endpoint (exact, Blossom)."""
    import networkx as nx

    g = nx.Graph()
    for s, t, d, c, fc, era in rows:
        w = 1.0 if weight_by is None else weight_by(d, c)
        g.add_edge(s, t, weight=w)
    m = nx.max_weight_matching(g, maxcardinality=True)
    return m, g


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pools", type=int, nargs="+", default=[200, 400, 600, 800])
    args = ap.parse_args()

    u = Universe()
    dup_names = {n for n, k in Counter(u.names).items() if k > 1}

    for pool_size in args.pools:
        print("\n" + "=" * 64)
        rows = scan(u, pool_size)
        buckets = tiered(rows)
        print("  tier coverage (pairs legal for each weekday band):")
        for name, *_ in TIERS:
            print("    %-4s %6d" % (name, len(buckets[name])))

        m, g = max_matching(rows)
        print("  distinct-endpoint puzzles (max matching): %d" % len(m))
        print("  ...covering %d of %d pool players" % (2 * len(m), pool_size))

        # How many of those matched pairs can each weekday band supply, if we
        # also demand no endpoint is ever reused across the whole schedule?
        per_tier = {}
        for name, *_ in TIERS:
            if not buckets[name]:
                per_tier[name] = 0
                continue
            mt, _ = max_matching(buckets[name])
            per_tier[name] = len(mt)
        print("  per-band max matching (band in isolation):")
        print("   ", "  ".join("%s=%d" % (k, v) for k, v in per_tier.items()))
        # A schedule needs each band weekly; the binding constraint is the
        # scarcest band.
        scarcest = min(per_tier.values())
        print("  weeks sustainable if no endpoint EVER repeats: ~%d" % scarcest)

        amb = sum(1 for s, t, *_ in rows if u.names[s] in dup_names or u.names[t] in dup_names)
        print("  pairs touching an ambiguous (duplicated) name: %d" % amb)

    # persist the richest scan for the scheduler
    rows = scan(u, max(args.pools), verbose=False)
    out = os.path.join(ROOT, "data", "candidates.json")
    with open(out, "w") as fh:
        json.dump(
            [{"s": s, "t": t, "d": d, "chains": c, "fchains": fc, "era": era,
              "tiers": tiers_for(d, c)}
             for s, t, d, c, fc, era in rows],
            fh, separators=(",", ":"),
        )
    print("\nwrote %s (%d pairs)" % (out, len(rows)))


if __name__ == "__main__":
    main()
