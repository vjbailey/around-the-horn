"""Build the client-side data artifacts from the Lahman/Chadwick CSVs.

Emits into web/data/:
  players.json   names, active years, primary team, fame score
  teams.json     team-season pool (display name + year) used for link labels
  roster.bin     binary player -> team-season incidence lists (the bipartite spine)
  meta.json      counts + source provenance

Teammate model is season-level *plus an appearance window*. Sharing a teamID
and a yearID is not enough: someone traded away in May was never a teammate of
the man who replaced him in July, and that artifact affects ~15.6% of
season-level pairs. build_spans.py records the first and last date each player
appeared for each club; this bakes those into roster.bin so the client can
apply the overlap test itself.

Windows are appearance windows, not roster tenure -- a player is on the roster
before his first game and after his last -- so the test pads both ends by
PAD_DAYS and exempts windows shorter than MIN_SPAN_DAYS, where the appearance
window says little about how long someone was actually around.
"""

import csv
import datetime
import json
import os
import struct
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, "data", "raw")
OUT = os.path.join(ROOT, "web", "data")

SOURCE = "chadwickbureau/baseballdatabank @ xorq-labs mirror (2021 databank)"

# Appearance windows are stored as a day offset from 1 March (so the whole
# season fits in a byte). 0/255 means "no window recorded" -- pre-1980 seasons
# and the current one -- and always overlaps.
DAY_ORIGIN = 59          # 1 March, in day-of-year terms
SPAN_UNKNOWN = (0, 255)
PAD_DAYS = 10            # roster time either side of a player's appearances
MIN_SPAN_DAYS = 21       # shorter windows say too little; exempt them


def read(name):
    """Read a vendored CSV plus its *_recent.csv top-up, if present.

    The vendored databank ends at 2021; pipeline/update_recent.py writes the
    newer seasons alongside it rather than rewriting the source files.
    """
    rows = []
    stem, ext = os.path.splitext(name)
    for path in (os.path.join(RAW, name),
                 os.path.join(RAW, stem + "_recent" + ext)):
        if not os.path.exists(path):
            continue
        with open(path, newline="", encoding="utf-8-sig") as fh:
            rows.extend(csv.DictReader(fh))
    return rows


def main():
    os.makedirs(OUT, exist_ok=True)

    people = read("People.csv")
    appearances = read("Appearances.csv")
    teams = read("Teams.csv")
    allstar = read("AllstarFull.csv")
    hof = read("HallOfFame.csv")
    awards = read("AwardsPlayers.csv")

    # --- team-seasons -------------------------------------------------------
    # A team-season is keyed (yearID, teamID). Display name is the franchise's
    # name *in that year* so 1907 Boston reads "Boston Americans", not "Red Sox".
    ts_index = {}          # (year, teamID) -> ts idx
    ts_name_idx = []       # ts idx -> team-name pool idx
    ts_year = []           # ts idx -> year
    ts_team_code = []      # ts idx -> teamID
    ts_franch = []         # ts idx -> franchID
    name_pool = {}
    names_ordered = []

    for row in teams:
        key = (int(row["yearID"]), row["teamID"])
        if key in ts_index:
            continue
        nm = row["name"]
        if nm not in name_pool:
            name_pool[nm] = len(names_ordered)
            names_ordered.append(nm)
        ts_index[key] = len(ts_year)
        ts_name_idx.append(name_pool[nm])
        ts_year.append(key[0])
        ts_team_code.append(row["teamID"])
        ts_franch.append(row["franchID"])

    # --- players ------------------------------------------------------------
    # Only players who actually appear in Appearances are part of the universe;
    # People.csv also carries managers/umpires-only entries.
    stints = defaultdict(set)          # playerID -> {ts idx}
    games_on = defaultdict(lambda: defaultdict(int))  # playerID -> ts idx -> G
    career_games = defaultdict(int)
    missing_ts = 0

    # Primary position, for the middle rung of the hint ladder. Derived from
    # games by position; players new since 2021 have no Appearances breakdown,
    # so update_recent.py carries their position on People_recent instead.
    POS_COLS = [("G_p", "P"), ("G_c", "C"), ("G_1b", "1B"), ("G_2b", "2B"),
                ("G_3b", "3B"), ("G_ss", "SS"), ("G_lf", "LF"), ("G_cf", "CF"),
                ("G_rf", "RF"), ("G_dh", "DH")]
    pos_games = defaultdict(lambda: defaultdict(int))

    # (playerID, ts) -> (lo, hi) day offsets, from build_spans.py
    def day_offset(iso):
        doy = datetime.date.fromisoformat(iso).timetuple().tm_yday
        return max(1, min(254, doy - DAY_ORIGIN))

    spans = {}
    for name in ("Spans.csv", "Spans_recent.csv"):
        path = os.path.join(RAW, name)
        if not os.path.exists(path):
            continue
        with open(path, newline="", encoding="utf-8-sig") as fh:
            for row in csv.DictReader(fh):
                ts = ts_index.get((int(row["yearID"]), row["teamID"]))
                if ts is None:
                    continue
                spans[(row["playerID"], ts)] = (
                    day_offset(row["first"]), day_offset(row["last"]))
    if spans:
        print("appearance windows loaded:", len(spans))
    else:
        print("no Spans.csv -- falling back to season-level teammates")

    for row in appearances:
        key = (int(row["yearID"]), row["teamID"])
        ts = ts_index.get(key)
        if ts is None:
            missing_ts += 1
            continue
        pid = row["playerID"]
        stints[pid].add(ts)
        g = int(row["G_all"] or 0)
        games_on[pid][ts] += g
        career_games[pid] += g
        for col, code in POS_COLS:
            v = row.get(col)
            if v:
                pos_games[pid][code] += int(v or 0)

    # --- recognizability inputs --------------------------------------------
    allstar_n = defaultdict(int)
    for row in allstar:
        allstar_n[row["playerID"]] += 1

    # HOF induction, weighted by peak ballot share. Vote share is the one
    # stature signal that works across eras: the All-Star game only starts in
    # 1933, so Ruth/Cobb/Wagner score near-zero on selections alone.
    hof_players = {}
    for row in hof:
        if row["inducted"] != "Y" or row["category"] != "Player":
            continue
        try:
            pct = int(row["votes"]) / int(row["ballots"])
        except (ValueError, TypeError, ZeroDivisionError):
            pct = 0.5  # Veterans Committee et al: no ballot, assume mid
        hof_players[row["playerID"]] = max(hof_players.get(row["playerID"], 0), pct)

    BIG_AWARDS = {
        "Most Valuable Player": 12,
        "Cy Young Award": 12,
        "Rookie of the Year": 6,
        "Triple Crown": 10,
        "World Series MVP": 5,
        "Gold Glove": 2,
        "Silver Slugger": 2,
    }
    award_pts = defaultdict(int)
    for row in awards:
        award_pts[row["playerID"]] += BIG_AWARDS.get(row["awardID"], 0)

    person = {r["playerID"]: r for r in people}

    pids = sorted(stints.keys())
    p_index = {pid: i for i, pid in enumerate(pids)}

    POS_POOL = ["", "P", "C", "1B", "2B", "3B", "SS", "LF", "CF", "RF", "DH"]
    pos_idx = {code: i for i, code in enumerate(POS_POOL)}
    names, y0s, y1s, fames, primaries, positions = [], [], [], [], [], []
    for pid in pids:
        rec = person.get(pid, {})
        first = (rec.get("nameFirst") or "").strip()
        last = (rec.get("nameLast") or "").strip()
        display = (first + " " + last).strip() or pid
        years = sorted(ts_year[t] for t in stints[pid])
        y0, y1 = years[0], years[-1]

        # primary team = most games played for a single franchise
        by_franch = defaultdict(int)
        for ts, g in games_on[pid].items():
            by_franch[ts_franch[ts]] += g
        best_franch = max(by_franch, key=lambda f: by_franch[f]) if by_franch else ""
        best_ts = max(
            (t for t in stints[pid] if ts_franch[t] == best_franch),
            key=lambda t: games_on[pid][t],
            default=next(iter(stints[pid])),
        )

        fame = 0.0
        if pid in hof_players:
            fame += 40 + 50 * hof_players[pid]
        fame += min(allstar_n[pid] * 4, 60)
        fame += min(award_pts[pid], 45)
        fame += min(career_games[pid] / 100.0, 25)
        # Mild recency tilt: a modern audience recognizes recent stars more
        # readily, without erasing pre-war legends.
        if y1 >= 2000:
            fame += 8
        elif y1 >= 1975:
            fame += 4

        counts = pos_games.get(pid)
        if counts:
            code = max(counts, key=counts.get)
        else:
            code = (rec.get("pos") or "").upper()
            if code in ("LHP", "RHP", "SP", "RP", "P"):
                code = "P"
            elif code == "OF":
                code = "CF"
        positions.append(pos_idx.get(code, 0))

        names.append(display)
        y0s.append(y0)
        y1s.append(y1 - y0)  # delta-encoded, keeps the JSON small
        fames.append(round(fame, 1))
        primaries.append(ts_name_idx[best_ts])

    # --- roster.bin ---------------------------------------------------------
    # offsets: (nPlayers+1) uint32, then the flat uint16 team-season ids.
    offsets = [0]
    flat = []
    span_lo = []
    span_hi = []
    for pid in pids:
        for ts in sorted(stints[pid]):
            flat.append(ts)
            lo, hi = spans.get((pid, ts), SPAN_UNKNOWN)
            span_lo.append(lo)
            span_hi.append(hi)
        offsets.append(len(flat))

    assert len(ts_year) < 65536, "team-season count no longer fits in uint16"

    with open(os.path.join(OUT, "roster.bin"), "wb") as fh:
        fh.write(b"SDBB")
        fh.write(struct.pack("<III", 2, len(pids), len(flat)))
        fh.write(struct.pack("<%dI" % len(offsets), *offsets))
        fh.write(struct.pack("<%dH" % len(flat), *flat))
        fh.write(struct.pack("<%dB" % len(flat), *span_lo))
        fh.write(struct.pack("<%dB" % len(flat), *span_hi))

    with open(os.path.join(OUT, "players.json"), "w", encoding="utf-8") as fh:
        json.dump(
            {
                "names": "\n".join(names),
                "y0": y0s,
                "dy": y1s,
                "fame": fames,
                "team": primaries,
                "pos": positions,
                "posPool": POS_POOL,
                "spanPad": PAD_DAYS,
                "spanMin": MIN_SPAN_DAYS,
            },
            fh,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    with open(os.path.join(OUT, "teams.json"), "w", encoding="utf-8") as fh:
        json.dump(
            {"pool": names_ordered, "name": ts_name_idx, "year": ts_year},
            fh,
            ensure_ascii=False,
            separators=(",", ":"),
        )

    with open(os.path.join(OUT, "meta.json"), "w", encoding="utf-8") as fh:
        json.dump(
            {
                "source": SOURCE,
                "players": len(pids),
                "teamSeasons": len(ts_year),
                "stints": len(flat),
                "yearRange": [min(ts_year), max(ts_year)],
            },
            fh,
            indent=2,
        )

    # playerID <-> index map stays server-side; the pipeline needs it, the
    # client never does.
    with open(os.path.join(ROOT, "data", "player_ids.json"), "w") as fh:
        json.dump(pids, fh, separators=(",", ":"))

    dated = sum(1 for k in range(len(flat)) if (span_lo[k], span_hi[k]) != SPAN_UNKNOWN)
    print("stints with a window: %d of %d (%.0f%%)"
          % (dated, len(flat), 100 * dated / max(len(flat), 1)))
    print("players       ", len(pids))
    print("team-seasons  ", len(ts_year))
    print("stints        ", len(flat))
    print("year range    ", min(ts_year), "-", max(ts_year))
    if missing_ts:
        print("appearances with no Teams.csv row:", missing_ts)
    for f in ("players.json", "teams.json", "roster.bin"):
        p = os.path.join(OUT, f)
        print("%-14s %8.1f KB" % (f, os.path.getsize(p) / 1024))


if __name__ == "__main__":
    sys.exit(main())
