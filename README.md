# Around the Horn

A daily browser puzzle: connect two MLB players through a chain of real
teammates. One puzzle per day, identical for everyone.

Named for the infielders relaying the ball after an out: it travels player to
player around the bases, which is exactly the mechanic — and exactly what the
board animates when you solve one.

## Running it

```sh
cd web && python3 -m http.server 8777      # then open http://127.0.0.1:8777/
```

It is a static site — no build step, no server-side dependency for gameplay.

## Architecture

The whole game runs client-side. Rather than shipping a materialized teammate
edge list (~1.5M pairs), the browser gets the **bipartite roster index**:
player ↔ team-season incidences, 118k entries. Every query the game needs is a
short walk over that.

| Query | Method |
|---|---|
| Are A and B teammates? | merge their sorted team-season lists |
| Is this guess a dead end? | one BFS from the target, blocking the chain |
| Which names can I still play? | same BFS; every teammate still reaching the target |

Client payload is ~945 KB raw across four files; a full BFS is ~8 ms and an
autocomplete query ~1.2 ms, so no solution ever needs to be shipped or fetched.

```
data/raw/          vendored CSVs (Lahman/Chadwick base + StatsAPI top-up)
pipeline/          offline: build the index, tier pairs, schedule the year
web/               the game (static)
web/data/          generated artifacts: players.json, teams.json, roster.bin, puzzles.json
```

## Rebuilding the data

```sh
cd pipeline
python3 update_recent.py            # top up 2022+ from MLB StatsAPI (cached)
python3 build_index.py              # CSVs -> web/data artifacts
python3 schedule.py --pool 1400 --cooldown 90 --days 730 --start 2026-08-01
node ../web/engine.test.mjs         # verify the client engine against the bank
```

### Where the data comes from

The canonical `chadwickbureau/baseballdatabank` repo is **gone** (404, and
absent from the org listing). The base CSVs are vendored from the `xorq-labs`
fork, frozen at the **2021** databank; `cbwinslow/baseballdatabank` is an
identical backup. They are CC-BY-SA, so vendoring is fine — and preferable to
depending on a stranger's fork staying up.

Seasons **2022 onward come from MLB's public StatsAPI** (`update_recent.py`),
joined back to Lahman ids through the Chadwick register's `key_mlbam` →
`key_bbref` crosswalk. This matters for more than currency: with data ending in
2021, Judge and Ohtani were 3-season stubs, so the fame score buried them
(Ohtani ranked 432nd) and the scheduler reached down to rank ~1200 to fill
modern slots, surfacing pairs like *Melky Cabrera → Alex Rios*. After the
top-up Ohtani ranks 171st and Judge 160th.

Roster membership is read from **full-season rosters**, not the season-stats
endpoint: stats collapses a traded player onto a single team (24 multi-team
players in 2025 against ~90 in reality), and each omission is a lost teammate
edge.

Still stale: `HallOfFame.csv` ends at 2021, so inductees from 2022 on
(Ortiz, Rolen, Beltré, Helton, Mauer, Ichiro, Sabathia, Wagner) get no
induction credit in the fame score.

## Design notes

**Difficulty is chain scarcity, not chain length.** The spec proposed distance-3
puzzles for Thursday–Sunday. The graph will not support it: among famous
players who overlapped in the modern era, essentially *everyone* is exactly two
hops apart — 26,240 modern pairs at distance 2 against 101 at distance 3, and
just **two** post-1980 same-generation pairs at distance 3. Free agency, bigger
rosters and more movement mean a few well-travelled journeymen connect
everybody. So difficulty is driven by *how many* shortest chains exist (40+ on
Monday, 4–8 on Sunday), which is what §6 says is primary anyway.

This costs less than it sounds, because **the board is not fixed-length**. A
player adds links one at a time and may wander; a distance-2 puzzle only means
a 2-link solution *exists*. Since scoring counts assists alone and explicitly
refuses to penalize chain length, a circuitous route costs nothing.

**Assists narrow, they never solve.** One press lists everyone who played with
your current name — 300–480 of them, best-known first — and says nothing about
which of them reaches the target. That part is the puzzle: on a Sunday only 6
of ~286 work. It removes the failure that isn't fun (being unable to name a
single teammate) without touching the one that is.

Assists never fill a link; only "give up" does. That keeps a single selection
mechanism, so there is never a second place to click or any doubt that you are
the one picking. Links are marked `self`, `assisted` or `revealed`
(🟩 / 🟨 / 🟥 in the share strip). This departs from §4, which specifies a hint
as filling a link outright — with two-hop puzzles that would end the game on
the first press.

**Rule 4 is vacuous, and the copy says so.** The no-dead-ends guarantee never
fires: across the whole bank, 0 of 267,936 candidate moves were dead ends. The
graph is dense enough that any teammate keeps a route open. The check stays in
the engine as insurance against a change of data, but the how-to no longer
teaches it — the rules collapse to one line, *name anyone who played with the
last player*.

**Era is the second difficulty axis.** Recent pairs early in the week, older
pairs later — which is both a design choice and what the graph supports, since
sparse pairs only exist in older eras. Weekly marginals land near 30% 2020s /
25% 2010s / 20% 2000s / 15% 1990s / 10% 1980s.

**Recognizability is ranked within era, not globally.** Fame accrues over a
career, so an active star always sits mid-table against retired Hall of Famers.
Ranking within era removes that bias. The fame score itself blends HOF vote
share (era-neutral — the All-Star game only starts in 1933, so Ruth and Cobb
score near-zero on selections), All-Star nods, major awards, career games and a
mild recency tilt.

**Two guardrails beyond the spec.** A pair must have a shortest chain routed
entirely through *recognizable connectors*, or it is technically solvable but
not humanly so (this killed `Ross Youngs → John Clarkson`). And both endpoints
must be within 12 years of each other by career midpoint, or the midpoint
average cheerfully labels `Chipper Jones + Mickey Mantle` as an "1980s" pair.

## Backend

None is required. Only the cross-player assist distribution and cross-device
streak sync need one, and both sit behind the adapter in `results.js` with a
synthetic fallback that is labelled as simulated in the UI. Point `API_BASE` at
a real endpoint and nothing else changes:

```
POST {API_BASE}/result  {date, assists, links, gaveUp}
     -> {date, histogram: {"0": n, ...},      // assists used
         lengths:   {"2": n, "3": n, ...},    // links taken
         total, percentile}
GET  {API_BASE}/stats?date=YYYY-MM-DD
     -> {date, histogram, lengths, total}
```

Daily rollover is 08:00 UTC (04:00 ET) — one fixed global reset, so "today's
puzzle" is unambiguous everywhere.

## Supply

With a 90-day endpoint cooldown the bank fills **730/730 days** with zero era
fallbacks. If no endpoint may *ever* repeat, a perfect matching exists at every
pool size (the pool, not the graph, is the constraint) and the weekday quota mix
caps a top-600 pool at ~277 puzzles.

## Look

Vintage scorecard: cream stock, ink, ruled lines, red pencil for labels, green
for a link you closed yourself and clay for one an assist helped with. Player
names are set in a slab serif, every number in tabular mono, and endpoints are
bracketed in double rules so the two ends of the board never read as
interchangeable. Dark mode is the same scorecard printed on carbon. All
foreground pairings clear WCAG AA on both grounds.

## Open

- **Teammate granularity** is season-level, so a mid-season trade can create a
  false link (A leaves in May, B arrives in June, both "1998 Team X"). Tightening
  needs roster dates.
- Endpoints skip players with duplicate display names; disambiguation by career
  span and primary club exists in autocomplete, so they could be allowed.
- Difficulty bands and the era mix are starting points, to be tuned in playtest.
