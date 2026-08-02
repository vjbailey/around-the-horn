"""Bring the teammate data up to date from MLB's public StatsAPI.

The vendored Lahman/Chadwick CSVs stop at 2021 (the canonical databank repo was
pulled). This tops them up with 2022+ so modern stars have real careers instead
of 3-season stubs, which is what was starving the recent-era endpoint pool.

Membership comes from full-season rosters rather than the season-stats endpoint:
stats collapses a traded player onto one team (24 multi-team players in 2025,
against ~90 in reality), and every one of those omissions is a lost teammate
edge. Games played come from the stats endpoint, for the fame score.

Writes *_recent.csv alongside the vendored files; build_index.py merges them.
Responses are cached under data/api_cache so re-runs are cheap and polite.
"""

import argparse
import csv
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, "data", "raw")
CACHE = os.path.join(ROOT, "data", "api_cache")
API = "https://statsapi.mlb.com/api/v1"
REGISTER = "https://raw.githubusercontent.com/chadwickbureau/register/master/data"

# MLB team id -> Lahman teamID. Ids are stable; Lahman codes keep franchise
# continuity across relocations and renames (Cleveland 2022 rename, the
# Athletics' move to Sacramento in 2025), so the graph does not fracture.
TEAM_MAP = {
    108: "ANA", 109: "ARI", 110: "BAL", 111: "BOS", 112: "CHN", 113: "CIN",
    114: "CLE", 115: "COL", 116: "DET", 117: "HOU", 118: "KCA", 119: "LAN",
    120: "WAS", 121: "NYN", 133: "OAK", 134: "PIT", 135: "SDN", 136: "SEA",
    137: "SFN", 138: "SLN", 139: "TBA", 140: "TEX", 141: "TOR", 142: "MIN",
    143: "PHI", 144: "ATL", 145: "CHA", 146: "MIA", 147: "NYA", 158: "MIL",
}


def fetch(url, cache_key, ttl_days=7, allow_missing=False):
    """GET with an on-disk cache. Current-season data expires, history doesn't.

    allow_missing tolerates a 404 -- awards for an in-progress season do not
    exist yet, which is expected rather than an error.
    """
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, cache_key + ".json")
    if os.path.exists(path):
        age = (time.time() - os.path.getmtime(path)) / 86400
        if ttl_days is None or age < ttl_days:
            with open(path) as fh:
                return json.load(fh)
    for attempt in range(4):
        try:
            with urllib.request.urlopen(url, timeout=45) as resp:
                data = json.load(resp)
            break
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and allow_missing:
                return {}
            if attempt == 3:
                raise RuntimeError("failed: %s (%s)" % (url, exc))
            time.sleep(1.5 * (attempt + 1))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            if attempt == 3:
                raise RuntimeError("failed: %s (%s)" % (url, exc))
            time.sleep(1.5 * (attempt + 1))
    with open(path, "w") as fh:
        json.dump(data, fh)
    time.sleep(0.12)          # be polite to a free public API
    return data


def load_crosswalk():
    """mlbam id -> (bbref id, first, last). Chadwick shards by uuid prefix."""
    out = {}
    os.makedirs(CACHE, exist_ok=True)
    for shard in "0123456789abcdef":
        path = os.path.join(CACHE, "register-%s.csv" % shard)
        if not os.path.exists(path):
            url = "%s/people-%s.csv" % (REGISTER, shard)
            sys.stderr.write("  fetching register shard %s\n" % shard)
            urllib.request.urlretrieve(url, path)
        with open(path, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                mlbam = row.get("key_mlbam") or ""
                if not mlbam.strip():
                    continue
                out[int(float(mlbam))] = (
                    (row.get("key_bbref") or "").strip(),
                    (row.get("name_first") or "").strip(),
                    (row.get("name_last") or "").strip(),
                )
    return out


def read_csv(name):
    with open(os.path.join(RAW, name), newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-season", type=int, default=2022)
    ap.add_argument("--to-season", type=int, default=None)
    args = ap.parse_args()

    to_season = args.to_season or time.gmtime().tm_year
    seasons = list(range(args.from_season, to_season + 1))
    print("updating seasons %d-%d" % (seasons[0], seasons[-1]))

    people = read_csv("People.csv")
    by_bbref = {r["bbrefID"]: r["playerID"] for r in people if r.get("bbrefID")}
    known_ids = {r["playerID"] for r in people}
    print("  base: %d players" % len(people))

    print("  loading Chadwick crosswalk...")
    xwalk = load_crosswalk()
    print("  crosswalk: %d mlbam ids" % len(xwalk))

    new_people = {}          # playerID -> row
    appearances = []         # (year, teamID, lgID, playerID, G)
    teams_rows = []
    unresolved = set()

    def resolve(mlbam, full_name):
        """mlbam -> Lahman playerID, minting one for players new since 2021."""
        bbref, first, last = xwalk.get(mlbam, ("", "", ""))
        if bbref and bbref in by_bbref:
            return by_bbref[bbref]
        pid = bbref or ("mlb%07d" % mlbam)
        if pid not in known_ids and pid not in new_people:
            if not (first or last):
                parts = full_name.rsplit(" ", 1)
                first, last = (parts[0], parts[-1]) if len(parts) > 1 else ("", full_name)
            new_people[pid] = {
                "playerID": pid, "nameFirst": first, "nameLast": last,
                "bbrefID": bbref, "mlbamID": mlbam,
            }
        return pid

    # games played, for the fame score
    games = defaultdict(int)
    for season in seasons:
        for group in ("hitting", "pitching"):
            url = ("%s/stats?stats=season&season=%d&sportId=1&playerPool=ALL"
                   "&limit=4000&group=%s" % (API, season, group))
            ttl = 1 if season >= to_season else None
            data = fetch(url, "stats-%d-%s" % (season, group), ttl_days=ttl)
            for st in data.get("stats", []):
                for sp in st.get("splits", []):
                    g = sp.get("stat", {}).get("gamesPlayed") or 0
                    games[(season, sp["player"]["id"])] = max(
                        games[(season, sp["player"]["id"])], int(g))

    for season in seasons:
        ttl = 1 if season >= to_season else None
        tdata = fetch("%s/teams?sportId=1&season=%d" % (API, season),
                      "teams-%d" % season, ttl_days=ttl)
        n_players = 0
        for team in tdata.get("teams", []):
            mlb_id = team["id"]
            team_id = TEAM_MAP.get(mlb_id)
            if team_id is None:
                sys.stderr.write("  WARN unmapped team %s %s\n" % (mlb_id, team.get("name")))
                continue
            lg = "AL" if team.get("league", {}).get("id") == 103 else "NL"
            teams_rows.append({
                "yearID": season, "teamID": team_id, "lgID": lg,
                "franchID": team_id, "name": team.get("name", ""),
            })
            roster = fetch(
                "%s/teams/%d/roster?rosterType=fullSeason&season=%d"
                % (API, mlb_id, season),
                "roster-%d-%d" % (season, mlb_id), ttl_days=ttl)
            for entry in roster.get("roster", []):
                person = entry["person"]
                pid = resolve(person["id"], person.get("fullName", ""))
                # Positions are not in Appearances for players new since 2021,
                # so take them from the roster entry.
                pos = (entry.get("position") or {}).get("abbreviation")
                if pos and pid in new_people:
                    new_people[pid].setdefault("posCounts", {})
                    new_people[pid]["posCounts"][pos] = \
                        new_people[pid]["posCounts"].get(pos, 0) + 1
                if pid is None:
                    unresolved.add(person["id"])
                    continue
                appearances.append({
                    "yearID": season, "teamID": team_id, "lgID": lg,
                    "playerID": pid,
                    "G_all": games.get((season, person["id"]), 0),
                })
                n_players += 1
        print("  %d: %d team-player rows" % (season, n_players))

    # --- awards & All-Star selections -------------------------------------
    # Without these, the fame score treats Judge's 2022 MVP and Ohtani's MVPs
    # as nonexistent, which buries exactly the players a modern audience knows.
    AWARD_MAP = {
        "ALMVP": "Most Valuable Player", "NLMVP": "Most Valuable Player",
        "ALCY": "Cy Young Award", "NLCY": "Cy Young Award",
        "ALROY": "Rookie of the Year", "NLROY": "Rookie of the Year",
        "WSMVP": "World Series MVP",
        "ALGG": "Gold Glove", "NLGG": "Gold Glove",
        "ALSS": "Silver Slugger", "NLSS": "Silver Slugger",
    }
    award_rows, allstar_rows = [], []
    for season in seasons:
        ttl = 1 if season >= to_season else None
        for award_id, lahman_name in AWARD_MAP.items():
            data = fetch("%s/awards/%s/recipients?season=%d" % (API, award_id, season),
                         "award-%s-%d" % (award_id, season), ttl_days=ttl,
                         allow_missing=True)
            for rec in data.get("awards", []):
                player = rec.get("player") or {}
                if not player.get("id"):
                    continue
                award_rows.append({
                    "playerID": resolve(player["id"], player.get("nameFirst", "")),
                    "awardID": lahman_name, "yearID": season,
                    "lgID": "AL" if award_id.startswith("AL") else "NL",
                    "tie": "", "notes": "",
                })
        for award_id in ("ALAS", "NLAS"):
            data = fetch("%s/awards/%s/recipients?season=%d" % (API, award_id, season),
                         "award-%s-%d" % (award_id, season), ttl_days=ttl,
                         allow_missing=True)
            for rec in data.get("awards", []):
                player = rec.get("player") or {}
                if not player.get("id"):
                    continue
                allstar_rows.append({
                    "playerID": resolve(player["id"], player.get("nameFirst", "")),
                    "yearID": season, "gameNum": 0, "gameID": "",
                    "teamID": "", "lgID": award_id[:2], "GP": 1, "startingPos": "",
                })

    def write(name, rows, fields):
        path = os.path.join(RAW, name)
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            w.writerows(rows)
        print("  wrote %-26s %6d rows" % (name, len(rows)))

    write("Appearances_recent.csv", appearances,
          ["yearID", "teamID", "lgID", "playerID", "G_all"])
    write("Teams_recent.csv", teams_rows,
          ["yearID", "teamID", "lgID", "franchID", "name"])
    write("AwardsPlayers_recent.csv", award_rows,
          ["playerID", "awardID", "yearID", "lgID", "tie", "notes"])
    write("AllstarFull_recent.csv", allstar_rows,
          ["playerID", "yearID", "gameNum", "gameID", "teamID", "lgID",
           "GP", "startingPos"])
    for row in new_people.values():
        counts = row.pop("posCounts", None)
        row["pos"] = max(counts, key=counts.get) if counts else ""
    write("People_recent.csv", list(new_people.values()),
          ["playerID", "nameFirst", "nameLast", "bbrefID", "mlbamID", "pos"])
    print("  players new since 2021: %d" % len(new_people))
    if unresolved:
        print("  UNRESOLVED mlbam ids: %d" % len(unresolved))


if __name__ == "__main__":
    main()
