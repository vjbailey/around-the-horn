"""Derive per-stint appearance windows, so mid-season trades stop inventing
teammates.

The season-level model says two players are teammates if they share a teamID
and a yearID. That is wrong ~15.6% of the time: someone traded away in May was
never a teammate of the man who replaced him in July. This reads Retrosheet
day-by-day data (via chadwickbureau/retrosplits) and records, for every
player-team-season, the first and last date they appeared.

The windows are appearance windows, not roster tenure -- a player is on the
roster before his first game and after his last. So the client pads each window
and exempts short ones; see PAD_DAYS / MIN_SPAN_DAYS in build_index.py. The job
here is only to record the dates.

Streams each season rather than storing it: the source files are ~29MB each and
we need three columns.
"""

import argparse
import csv
import io
import os
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, "data", "raw")
CACHE = os.path.join(ROOT, "data", "api_cache")
SRC = ("https://raw.githubusercontent.com/chadwickbureau/retrosplits/"
       "master/daybyday/playing-%d.csv")

# retrosplits covers through 2025; the current season falls back to
# season-level, which is harmless because nobody has been traded away and
# replaced yet within it.
FIRST_SEASON = 1980
LAST_SEASON = 2025

# Retrosheet renamed Oakland when the club moved to Sacramento in 2025; the
# franchise keeps its Lahman code so the graph does not fracture.
TEAM_ALIASES = {"ATH": "OAK"}


def read_csv(name):
    rows = []
    stem, ext = os.path.splitext(name)
    for path in (os.path.join(RAW, name), os.path.join(RAW, stem + "_recent" + ext)):
        if os.path.exists(path):
            with open(path, newline="", encoding="utf-8-sig") as fh:
                rows.extend(csv.DictReader(fh))
    return rows


def retro_to_player():
    """retroID -> Lahman playerID, via People plus the Chadwick register."""
    people = read_csv("People.csv")
    out = {}
    by_bbref = {}
    for r in people:
        if r.get("retroID"):
            out[r["retroID"]] = r["playerID"]
        if r.get("bbrefID"):
            by_bbref[r["bbrefID"]] = r["playerID"]

    # Players who debuted after the 2021 databank have no retroID in People;
    # the register carries key_retro -> key_bbref for them.
    added = 0
    for shard in "0123456789abcdef":
        path = os.path.join(CACHE, "register-%s.csv" % shard)
        if not os.path.exists(path):
            continue
        with open(path, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                retro = (row.get("key_retro") or "").strip()
                bbref = (row.get("key_bbref") or "").strip()
                if retro and retro not in out and bbref in by_bbref:
                    out[retro] = by_bbref[bbref]
                    added += 1
    print("  retro ids: %d from People, +%d from the register" % (len(out) - added, added))
    return out


def retro_team_map():
    """(year, retro team key) -> Lahman teamID, plus a set of valid teamIDs.

    Teams_recent.csv (2022+) has no teamIDretro column, and Lahman team codes
    are Retrosheet-style anyway, so an unmapped key that is itself a known
    teamID resolves to itself.
    """
    out = {}
    known = set()
    for r in read_csv("Teams.csv"):
        known.add(r["teamID"])
        retro = (r.get("teamIDretro") or "").strip()
        if retro:
            out[(int(r["yearID"]), retro)] = r["teamID"]
    return out, known


def fetch_season(year):
    for attempt in range(4):
        try:
            return urllib.request.urlopen(SRC % year, timeout=300)
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt == 3:
                raise RuntimeError("failed season %d (%s)" % (year, exc))
            time.sleep(2.0 * (attempt + 1))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-season", type=int, default=FIRST_SEASON)
    ap.add_argument("--to-season", type=int, default=LAST_SEASON)
    ap.add_argument("--out", default="Spans.csv")
    args = ap.parse_args()

    people = retro_to_player()
    teams, known_teams = retro_team_map()

    out_path = os.path.join(RAW, args.out)
    written = 0
    unknown_people, unknown_teams = set(), set()

    with open(out_path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["yearID", "teamID", "playerID", "first", "last", "games"])
        for year in range(args.from_season, args.to_season + 1):
            span = {}
            t0 = time.time()
            with fetch_season(year) as resp:
                r = csv.reader(io.TextIOWrapper(resp, encoding="utf-8"))
                head = next(r)
                iD = head.index("game.date")
                iPh = head.index("season.phase")
                iT = head.index("team.key")
                iP = head.index("person.key")
                for row in r:
                    if row[iPh] != "R":          # regular season only
                        continue
                    key = (row[iT], row[iP])
                    d = row[iD]
                    s = span.get(key)
                    if s is None:
                        span[key] = [d, d, 1]
                    else:
                        if d < s[0]:
                            s[0] = d
                        if d > s[1]:
                            s[1] = d
                        s[2] += 1

            for (team_key, person), (first, last, games) in span.items():
                pid = people.get(person)
                tid = teams.get((year, team_key))
                if tid is None:
                    alias = TEAM_ALIASES.get(team_key, team_key)
                    if alias in known_teams:
                        tid = alias         # Lahman codes are Retrosheet codes
                if pid is None:
                    unknown_people.add(person)
                    continue
                if tid is None:
                    unknown_teams.add((year, team_key))
                    continue
                w.writerow([year, tid, pid, first, last, games])
                written += 1
            print("  %d: %5d stints  (%.1fs)" % (year, len(span), time.time() - t0))
            sys.stdout.flush()

    print("wrote %s (%d rows)" % (out_path, written))
    if unknown_people:
        print("  unmapped retro ids: %d (e.g. %s)"
              % (len(unknown_people), sorted(unknown_people)[:4]))
    if unknown_teams:
        print("  unmapped teams: %s" % sorted(unknown_teams)[:6])


if __name__ == "__main__":
    main()
