# Data sources and attribution

Around the Horn is built on public baseball datasets. Each is credited below,
with the terms it is used under.

## Retrosheet

Appearance windows — the first and last date each player appeared for each club,
used to stop mid-season trades from inventing teammates — are derived from
Retrosheet day-by-day data, via the Chadwick Bureau's `retrosplits` release.

> The information used here was obtained free of charge from and is copyrighted
> by Retrosheet. Interested parties may contact Retrosheet at
> [www.retrosheet.org](https://www.retrosheet.org).

This notice is required by Retrosheet and also appears in the game itself.

## Lahman Baseball Database / Chadwick Bureau baseballdatabank

Players, teams, appearances, All-Star selections, awards and Hall of Fame
records through the 2021 season. Distributed under
[CC BY-SA 3.0](https://creativecommons.org/licenses/by-sa/3.0/).

The canonical `chadwickbureau/baseballdatabank` repository is no longer
available; the CSVs vendored in `data/raw/` came from a surviving fork and are
kept in-tree so the build does not depend on it remaining up.

## Chadwick Bureau Register

The `key_mlbam` / `key_bbref` / `key_retro` crosswalk used to join the datasets
together. Distributed under CC BY-SA.

## MLB Advanced Media

Seasons 2022 onward — rosters, games played, awards and All-Star selections —
come from MLB's public StatsAPI (`statsapi.mlb.com`). This is an unauthenticated
public endpoint. The data is copyright MLB Advanced Media; this project is a
free, non-commercial puzzle and is not affiliated with or endorsed by MLB.
