"""Teammate graph loading + shortest-path analysis for the puzzle pipeline.

Deliberately reads back web/data/roster.bin rather than re-deriving from CSV, so
the pipeline exercises the exact bytes the browser will parse.

Shortest-path counting note: if dist(s,t) == d, then *every* walk of length d
from s to t is a shortest path and is necessarily simple. So the number of
distinct shortest chains is just (A^d)[s,t], which we get from d sparse
matrix-vector products -- no path enumeration needed.
"""

import json
import os
import struct

import numpy as np
from scipy import sparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WEB = os.path.join(ROOT, "web", "data")


class Universe:
    def __init__(self):
        with open(os.path.join(WEB, "roster.bin"), "rb") as fh:
            buf = fh.read()
        assert buf[:4] == b"SDBB", "bad roster.bin magic"
        version, n_players, n_stints = struct.unpack_from("<III", buf, 4)
        assert version == 2, "unexpected roster.bin version %d" % version
        off = 16
        self.offsets = np.frombuffer(buf, "<u4", n_players + 1, off).astype(np.int64)
        off += (n_players + 1) * 4
        self.stints = np.frombuffer(buf, "<u2", n_stints, off).astype(np.int64)
        off += n_stints * 2
        self.span_lo = np.frombuffer(buf, "<u1", n_stints, off).astype(np.int64)
        off += n_stints
        self.span_hi = np.frombuffer(buf, "<u1", n_stints, off).astype(np.int64)

        p = json.load(open(os.path.join(WEB, "players.json"), encoding="utf-8"))
        self.names = p["names"].split("\n")
        self.y0 = np.array(p["y0"], dtype=np.int32)
        self.y1 = self.y0 + np.array(p["dy"], dtype=np.int32)
        self.fame = np.array(p["fame"], dtype=np.float64)
        self.primary = p["team"]
        # Must match engine.js exactly, or puzzles would be selected against a
        # different graph than the one the game plays on.
        self.span_pad2 = p.get("spanPad", 10) * 2
        self.span_min = p.get("spanMin", 21)

        t = json.load(open(os.path.join(WEB, "teams.json"), encoding="utf-8"))
        self.team_pool = t["pool"]
        self.ts_name = np.array(t["name"], dtype=np.int32)
        self.ts_year = np.array(t["year"], dtype=np.int32)

        self.ids = json.load(open(os.path.join(ROOT, "data", "player_ids.json")))
        self.n = n_players
        assert len(self.names) == n_players

        self._build_adjacency()

    def _build_adjacency(self):
        """Materialize the player-player teammate graph as a CSR matrix.

        An edge needs a shared club *and* overlapping appearance windows: a
        player traded away in May was never a teammate of the man who arrived
        in July. Windows under-state roster tenure, so short ones are exempt
        and the rest are padded -- the same rule the client applies.
        """
        n_ts = len(self.ts_year)
        # invert the player -> team-season index into team-season -> players
        players = np.repeat(np.arange(self.n), np.diff(self.offsets))
        order = np.argsort(self.stints, kind="stable")
        ts_sorted = self.stints[order]
        roster = players[order]
        lo_sorted = self.span_lo[order]
        hi_sorted = self.span_hi[order]
        bounds = np.searchsorted(ts_sorted, np.arange(n_ts + 1))

        pair_lo, pair_hi = [], []
        dropped = 0
        for ts in range(n_ts):
            mates = roster[bounds[ts]:bounds[ts + 1]]
            if len(mates) < 2:
                continue
            lo = lo_sorted[bounds[ts]:bounds[ts + 1]]
            hi = hi_sorted[bounds[ts]:bounds[ts + 1]]
            i, j = np.triu_indices(len(mates), k=1)
            short = ((hi[i] - lo[i]) < self.span_min) | ((hi[j] - lo[j]) < self.span_min)
            keep = short | ((lo[i] - hi[j] <= self.span_pad2)
                            & (lo[j] - hi[i] <= self.span_pad2))
            dropped += int((~keep).sum())
            i, j = i[keep], j[keep]
            if not len(i):
                continue
            a, b = mates[i], mates[j]
            pair_lo.append(np.minimum(a, b))
            pair_hi.append(np.maximum(a, b))
        self.dropped_pairs = dropped

        lo = np.concatenate(pair_lo)
        hi = np.concatenate(pair_hi)
        keys = np.unique(lo.astype(np.int64) * self.n + hi)
        lo, hi = keys // self.n, keys % self.n

        rows = np.concatenate([lo, hi])
        cols = np.concatenate([hi, lo])
        data = np.ones(len(rows), dtype=np.float64)
        self.A = sparse.csr_matrix((data, (rows, cols)), shape=(self.n, self.n))
        self.edges = len(lo)
        self.roster = roster
        self.ts_bounds = bounds

    # -- lookups -----------------------------------------------------------
    def find(self, name, year_hint=None):
        """Resolve a display name to an index; year_hint disambiguates."""
        hits = [i for i, n in enumerate(self.names) if n == name]
        if not hits:
            raise KeyError(name)
        if len(hits) > 1 and year_hint is not None:
            hits = [i for i in hits if self.y0[i] <= year_hint <= self.y1[i]] or hits
        if len(hits) > 1:
            hits.sort(key=lambda i: -self.fame[i])
        return hits[0]

    def teams_of(self, i):
        return self.stints[self.offsets[i]:self.offsets[i + 1]]

    def shared(self, i, j):
        """Team-seasons two players shared, as [(display name, year)]."""
        common = np.intersect1d(self.teams_of(i), self.teams_of(j))
        return [(self.team_pool[self.ts_name[t]], int(self.ts_year[t])) for t in common]

    def label(self, i, j):
        """Human link label: 'New York Yankees, 1923-1934'."""
        sh = self.shared(i, j)
        if not sh:
            return None
        by_team = {}
        for team, yr in sh:
            by_team.setdefault(team, []).append(yr)
        parts = []
        for team, yrs in by_team.items():
            yrs.sort()
            span = str(yrs[0]) if len(yrs) == 1 else "%d-%d" % (yrs[0], yrs[-1])
            parts.append("%s, %s" % (team, span))
        return " / ".join(parts)

    # -- path analysis -----------------------------------------------------
    def subgraph(self, mask):
        """Induced subgraph on `mask`, plus index maps to/from full indices."""
        keep = np.flatnonzero(mask)
        remap = np.full(self.n, -1, dtype=np.int64)
        remap[keep] = np.arange(len(keep))
        return self.A[keep][:, keep].tocsr(), remap, keep

    def walk_profile_on(self, A, s, n, max_depth=5):
        """walk_profile against an arbitrary adjacency matrix."""
        dist = np.full(n, -1, dtype=np.int8)
        count = np.zeros(n, dtype=np.float64)
        dist[s] = 0
        count[s] = 1.0
        x = np.zeros(n)
        x[s] = 1.0
        for d in range(1, max_depth + 1):
            x = A @ x
            fresh = (x > 0) & (dist < 0)
            dist[fresh] = d
            count[fresh] = x[fresh]
            x = x * fresh
            if not fresh.any():
                break
        return dist, count

    def walk_profile(self, s, max_depth=5):
        """Return (dist_vector, count_vector) of shortest distance and shortest-
        path count from s to every player, exploring up to max_depth hops."""
        dist = np.full(self.n, -1, dtype=np.int8)
        count = np.zeros(self.n, dtype=np.float64)
        dist[s] = 0
        count[s] = 1.0
        x = np.zeros(self.n)
        x[s] = 1.0
        for d in range(1, max_depth + 1):
            x = self.A @ x
            fresh = (x > 0) & (dist < 0)
            dist[fresh] = d
            count[fresh] = x[fresh]
            x = x * fresh  # drop non-shortest walks so deeper levels stay exact
            if not fresh.any():
                break
        return dist, count


if __name__ == "__main__":
    import time

    t0 = time.time()
    u = Universe()
    print("players %d  edges %d  (%.1fs)" % (u.n, u.edges, time.time() - t0))

    print("\n-- known teammate pairs (expect distance 1) --")
    pairs = [
        ("Babe Ruth", "Lou Gehrig"),
        ("Hank Aaron", "Eddie Mathews"),
        ("Derek Jeter", "Mariano Rivera"),
        ("Willie Mays", "Willie McCovey"),
    ]
    for a, b in pairs:
        i, j = u.find(a), u.find(b)
        print("  %-16s %-18s %s" % (a, b, u.label(i, j)))

    print("\n-- distances from Babe Ruth --")
    t0 = time.time()
    dist, count = u.walk_profile(u.find("Babe Ruth"))
    print("  bfs %.2fs" % (time.time() - t0))
    for name in ["Lou Gehrig", "Willie Mays", "Hank Aaron", "Nolan Ryan",
                 "Derek Jeter", "Mike Trout"]:
        i = u.find(name)
        print("  %-14s d=%d  shortest chains=%.0f" % (name, dist[i], count[i]))
    hist = np.bincount(dist[dist >= 0])
    print("  reach by hop:", list(enumerate(hist)))
