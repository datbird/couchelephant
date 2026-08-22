# SmartPass

A sidecar for a Plex DVR. It reads the guide into SQLite, shows you what Plex is
set up to record, and does one thing Plex gets wrong: recording a team's games
from the **live broadcast** rather than a rebroadcast.

## The problem it solves

Plex has a team pass. Ask it to record a team and it schedules every game. It
also, reliably, schedules the wrong airing when a game is repeated later in the
week. That is not a configuration mistake; it is a
[known, unfixed bug](https://forums.plex.tv/t/dvr-scheduling-wrong-airing-of-mlb-baseball-games/910902).

The maddening part is that the guide already says which airing is live:

```
41.1 KQGGDT  Sat 6:30PM   premiere: 1     <- the live broadcast
38.1 WQFFDT  Sun 5:00PM   (no flag)       <- Plex picks this one
```

SmartPass reads that flag, decides itself, and creates a **one-shot** recording
pinned to that exact channel and start time. Plex is left with nothing to choose
between, so it cannot choose wrong.

## What it does

- **Guide.** Pulls every airing into SQLite, refreshed on a schedule. Channel,
  exact times, premiere flag, DRM flag, teams, genres.
- **Recordings.** Mirrors what Plex has, including recurring rules like
  "All new episodes of X", so you can see everything in one place.
- **Search.** Across titles, series names and descriptions. Plex's own search
  box does not cover the EPG properly; this does.
- **Smart Sports Passes.** Follow a team, get the live broadcast.
- **Preview mode.** On by default. Passes work out what they would do and show
  you, writing nothing to Plex until you turn it off.

## Team identification

No string matching. Plex's guide carries structured team tags with stable ids:

```json
"Team": [{"id": 132, "tag": "Seattle Seahawks"},
         {"id": 236, "tag": "Kansas City Chiefs"}]
```

`/sections/<sports>/team` lists every team in your guide. This works for any
team in any sport, not just the one it was built for.

One wrinkle: a bulk section listing returns `Genre` but **not** `Team`. Team tags
only appear on per-programme metadata, so sports rows are enriched one at a time
after the bulk pull. Rows that already have teams are skipped, so it costs a
burst on first run and almost nothing after.

## Run it

```bash
docker build -t smartpass:1.0 .
docker run -d --name smartpass --restart unless-stopped \
  -p 8710:8710 -v /path/to/data:/data -e TZ=America/Chicago smartpass:1.0
```

Then open the UI, go to **Settings**, and give it your Plex address and token.
Everything else (EPG provider id, which sections hold Shows and Sports) is
discovered from the server.

`deploy.sh` ships, builds and recreates the container on a remote host. It
recreates rather than restarts on purpose: `docker restart` keeps a container
pinned to the image it was created with, so a rebuild is silently ignored.

## Settings

| Setting | Notes |
|---|---|
| Plex server | As addressed from inside the container |
| Plex token | Stored in the local database, never logged |
| Timezone | Display only. Guide data is stored as UTC, so changing it never moves a recording |
| Sync interval | How often the guide refreshes and passes re-evaluate |
| Preview mode | On by default. Nothing is written to Plex while it is on |

## Selection rules

For each game a pass matches:

1. Drop DRM airings. Nothing downstream can decrypt them.
2. Prefer an airing flagged `premiere`.
3. Among equals, take the earliest start.
4. Skip anything Plex or a previous run already has.

Every decision is written to `pass_actions`, including the skips and why, and
shown in the UI. It should never be a black box.

## Caveats

Plex's API is only partly documented and the server's real paths differ from the
published ones. Everything here was verified against a live server, but Plex can
change it. The SQLite file is a cache for everything except your passes and their
audit trail; those are the only rows that cannot be rebuilt from Plex.
