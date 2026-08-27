# Changelog

## Unreleased

### Added

- **CouchElephant now watches whether Plex is keeping its own guide up to
  date.** It can only choose from the airings Plex offers, and when Plex stops
  refreshing, nothing here breaks. Passes keep running, syncs keep succeeding,
  and the guide gets a day shorter every day until the game you wanted is past
  the end of it. Nothing errors. You find out the evening the recording does
  not happen.

  Plex is asked what it intends rather than assumed at. `/butler` gives the
  real interval of its guide refresh and whether it is switched on; the DVR
  gives when it last actually ran. A weekly task five days late is fine and a
  daily one five days late is broken, and now the app can tell the difference
  because Plex told it which this is.

  Four things raise a notice: the refresh task switched off, the guide not
  refreshed within twice its own interval, the guide reaching less than three
  days ahead, and Plex unreachable. A daily task that slipped one day is late
  rather than broken, and saying so would only teach you to ignore the badge.

  It shows as a badge on the sync button, not a fourth icon in the bar. A guide
  that has stopped moving is a sync problem. The badge is its own button, so
  reading what is wrong does not start a sync you did not ask for.

  A notice clears itself when the condition clears, and cannot be dismissed. A
  health warning you can click away is one you will click away. It remembers
  when the problem started, because "since when" is the question you ask on
  finding a stale guide four days late.

  Every sync also writes down when Plex last refreshed and how far its guide
  reached, so a guide that has stopped moving is visible the next day rather
  than the week after.

### Fixed

- **Plex renumbers its team ids, and a team pass did not survive it.** Measured
  on a live server: one guide refresh moved the Kansas City Chiefs from 236 to
  245 and the Seattle Seahawks from 132 to 244, on the same game with the same
  programme guid. A pass followed the stored id, so from that moment it matched
  nothing, and said nothing about it, because matching nothing is what a team
  with no games this week looks like. You would find out on a Sunday.

  The name is the identity and the id is a handle into whatever guide Plex
  currently holds. A pass is now repointed at today's id on every sync, and an
  airing is matched on the id **or** the team's name.

  Both halves are needed. `programs.teams` is enriched once and then preserved,
  so a cached programme keeps the old ids long after Plex has moved on:
  verified live, a pass correctly repointed to 245 matched zero airings against
  a cache still holding 236. Correcting the id alone would have made it worse.

  Names are compared on spelling only: case, accents and punctuation folded,
  every word kept. Not the catalogue's own folding, which also drops club words
  and would make "Real Madrid" match Atletico Madrid, and Cincinnati match FC
  Cincinnati. Where a pass and the guide spell a team differently, that is now
  settled at the source: repointing a pass also adopts Plex's own spelling, so
  a pass made from the shipped catalogue stops carrying a name the guide has
  never used.

- **Team names outside Latin script folded to nothing, so they were all one
  team.** Both folds ended with `[^a-z0-9]`, which does not narrow a Cyrillic,
  Greek, Japanese, Hebrew or Arabic name so much as delete it. An empty string
  is not a miss, it is a key. Three things followed. The catalogue answered VfB
  Stuttgart for every such name, because the alias "VFB" also folds away and
  claimed that key. Every non-Latin team was tagged Bundesliga. And four
  distinct teams in one guide collapsed into a single entry, so a pass for
  Zenit was repointed at the Hanshin Tigers and had its name overwritten to
  match.

  The fold now keeps anything Unicode calls alphanumeric, and every lookup
  refuses an empty key rather than looking it up.

  Combining marks come off only where the letter is ASCII. Beyond Latin they
  carry meaning: the Japanese dakuten is the difference between KA and GA, so
  stripping it turned the Hanshin Tigers into a word that is not "tigers".

  A name made entirely of club words, like "Athletic Club", keeps them rather
  than folding to nothing.

- **A team pass that can find no game now says so.** That silence is what let
  the renumbering hide. A team out of season trips it too, which is the point:
  being told a pass is idle is cheap, and finding out months late is not.

- **A sports programme with no teams was re-fetched from Plex every hour.**
  Enrichment asks Plex for the team tags a bulk listing does not carry, and
  skips any row that already has them. A row Plex has no teams for never gets
  any, so it qualified again on the next sync, and the one after that, for as
  long as it stayed in the guide. Most sport in a guide is not a game: a
  phone-in, a highlights show, a shop. On a 63-channel lineup that was 72
  requests an hour, about 1,700 a day, every one of them answering the same
  nothing. Every attempt is now dated in `programs.teams_tried_at`, whatever
  it found.

  The note expires after a day rather than settling it for good. A game can
  reach the guide before Plex tags it, and a permanent "no teams" would hide
  that game from a team pass for the rest of its run. A call that fails is not
  an answer and is not written down.

  The sync line now reports what it asked as well as what it found. "0 sports
  enriched" used to mean either "nothing to do" or "asked seventy times and
  found nothing".

## 1.0.1 - 2026-08-23

The first public release. Published as `ghcr.io/datbird/couchelephant` for
linux/amd64 and linux/arm64, and listed in Unraid Community Applications.

Nothing in the app is tied to one country. Networks, genres, content ratings
and channels are read from your own guide rather than from a list shipped
here. The team catalogue covers 1,310 teams across 18 leagues, European
football included. Times, dates, day names and the first day of the week all
follow the viewer's own locale, so the same install reads correctly in
Seattle, London and Vancouver.

One-shot recordings are identified by Plex's own subscription type rather than
by the English words in its title. A Plex server in German offers "Diese
Sendung", not "This Episode", and the app used to read the English and pick a
series rule instead: it would have recorded every airing rather than the one
game. Verified against a live server, which also answers type 15 for a team
subscription rather than 2.

Searching and filtering fold case across the whole of Unicode. SQLite stops at
Z, so a search for "muller" never found "MULLER" with an umlaut.

Setting up pulls the guide straight away rather than waiting for the sync
loop's next turn, and the empty grid says what it is doing. It used to read
"End of the channel list", which on a minute-old install looks like a broken
app rather than an early one.

The guide's day strip is written in the viewer's language and date order. It
was built on the server in English.

### Fixed

- **Following a team as a plain Plex rule made a rule for the whole league.**
  The panel lists the named team first; Plex lists the league first. The
  position in the shown list was sent where Plex's own index belonged, so
  "All Kansas City Chiefs Events" became "All NFL Football Events". The payload
  already carried the right index; it is now the thing sent. The fake server
  lists templates in Plex's order so the suite covers it.
- Saving a paused pass's settings no longer resumes it. A field that is not
  sent means unchanged.
- Channel artwork now exports and imports by file name. A row used to carry the
  absolute path of the machine it came from, which put `logos//data/logos/...`
  in the zip and a foreign path in the restored row.
- The backing store's own passwords (`pg_password`, `my_password`) are secrets
  like the Plex token, and stay out of an export unless asked for.
- The Plex token travels as a header, never in a URL, so it cannot end up in
  an error message or a log line.
- `%` and `_` in a search or a smart-filter value are literal, not wildcards.
- A search with `&` or `#` in it survives the redirect.
- The first-run timezone picker starts on UTC. UTC was the default but not in
  the list, so the browser showed the first entry, Africa/Abidjan.
- The export checkbox says "Include secrets" and names them; it covered more
  than the Plex token and the label had not kept up.

### Changed

- `web.py` is split into `app/routes/`, one module per screen, with the app,
  middleware and background loops left in `web.py`. `base.html` no longer
  carries a thousand lines each of CSS and JS; they are `static/css/app.css`
  and `static/js/app.js`, versioned like the other scripts.
- `ruff` lints the tree at the start of every test run (`ruff.toml`). Every
  `raise` inside an `except` now chains its cause. Public functions in the
  core modules carry type hints.
- Every Plex client is closed when its request is done. A handful of routes
  used to leak one connection each.
- Pass airings are selected in SQL (`json_each` on the team list) instead of
  loading every future airing and sifting it in Python. A pass list of forty
  no longer reads twenty thousand rows each.
- Pass history (`our_grabs`, `pass_actions`) is trimmed to sixty days after
  each sync. It was never trimmed.
- A signed-in account with the `user` role can use the guide, the schedule and
  the passes, and is refused at settings, accounts, backups, the backing store
  and export/import. Until now the role was recorded and nothing read it.
- The default timezone is UTC, not one person's. A fresh install asks anyway.
- The backing-store test no longer leaves a `couchelephant_probe` table behind,
  and a sync reads the remote once per store rather than twice.

### Added

- **Plex's own settings on every pass, smart filters included.** Padding before
  and after, resolution, partial airings, commercial detection. They are the
  one-shot template's, because that is what a pass books for each airing, so
  the recurring-only choices are no longer offered and then silently dropped.
- **A sports pass now arrives with padding filled in**, one minute before and
  thirty after, shown before you create it. A game that runs long used to be cut
  off at whatever time the guide claimed.
- Padding suggests up to 180 minutes and caps nothing. Plex sends the field as
  a plain integer with no allowed-values list, so any number you type works.
- Every Plex setting now shows Plex's own explanation of it, as a tooltip on a
  small mark beside the label. Inline it ran to twenty lines: "Detect
  commercials" alone pushed one row past six hundred pixels. The options stay
  two columns of single rows. The tooltip is drawn on the body and clamped to
  the window, because every panel it can appear in scrolls, and a scrolling
  box clips its own children.
- The two padding fields are last, in that order. Plex lists them in the
  middle, which split the pair people actually reach for.
- The option row and the setting renderer existed twice and had drifted. They
  are one component now, in `static/js/ce.js`, with a test that keeps it that
  way.
- **Export and import.** One zip with everything you decided: passes, the
  recordings they booked, channel artwork, settings and accounts. Readable JSON
  inside, on purpose. The Plex token is left out unless you ask.
- **Snapshot backups.** Jobs with their own folder, schedule and retention.
  Optional AES-256 that 7-Zip and Keka can open. The database files are copied
  with SQLite's online backup, so a snapshot taken mid-write is consistent.
  Restoring takes a copy of the current state first.
- **A backing store.** A live two-way copy in PostgreSQL, MySQL or another
  SQLite file, reconciled by three-way merge on demand or on a timer. An edit
  beats a delete; a conflict goes to the later timestamp. Restoring pulls down
  and never writes back, so restoring onto an empty machine cannot erase the
  copy it is restoring from.
- Passes now carry a `uid`, and the recordings they book carry `pass_uid`, so a
  pass means the same thing on another machine.
- **1,310 teams to follow, not 76.** Plex lists only the teams playing in the
  next eleven days. CouchElephant now ships its own catalogue: every NFL, NBA,
  MLB, NHL, WNBA, MLS and NWSL side, 929 NCAA schools, and the top two tiers of
  six football leagues. The picker filters by league and marks which teams are
  in the guide this week.
- A team can be followed before it plays. The pass says it is waiting, and
  starts the moment the team appears in the guide.
- Teams are no longer deleted when they stop playing, so the list grows over a
  season instead of shrinking to whoever is on this week.
- **Smart passes.** The add panel now offers **Smart Pass** (a sports team, or a
  smart filter) and **Programme or Series**.
- A smart filter is a nested tree of conditions. A group matches all, any or
  none of what it holds, and groups nest to any depth. It can ask about title,
  series, description, genre, content rating, year, kind, first shown, length,
  channel, network, high definition and live.
- Content rating and running time are now stored, so a filter can ask about
  them. Both fill in on the next guide sync.
- Every condition carries **or blank**, because the guide rates only part of
  itself and a missing value silently defeats a negative condition otherwise.
- The panel counts what a filter would record, and shows the first matches,
  before anything is created. A loose or large filter has to be confirmed a
  second time, with the number on the button.

### Tests

- A test suite, 195 checks, in `tests/`. It runs against a fake Plex server
  that reproduces the real one's quirks, and a browser suite drives the pages
  themselves.
- `tests/isolation.py` refuses to run unless every path is scratch and Plex is
  on localhost. A suite that can reach production eventually reaches it.
- `scripts/test.sh` runs the whole thing in a throwaway container.

### Fixed

- Scripts are asked for by build version. Without that a deploy never reached
  the browser, so a shipped fix could sit on the server unseen.
- The Schedule legend named a colour per side, and the rows it described had
  no marker on them.
- Two colours in the verdict chip were written as hex instead of coming from
  the token block. There is now an `--on-solid` token, and a test that fails if
  another one appears.

## 0.90

The first release worth showing anyone. It records the live broadcast, and it
explains itself.

### Recording

- Chooses the airing itself, preferring the one the guide flags as live, and
  pins the recording to that channel and start time so Plex cannot re-choose.
- Passes follow a team or a programme and keep matching new airings.
- A rule can be limited to several networks or channels, which Plex cannot
  express. The limit is applied before the airing is chosen, not after.
- The record panel shows Plex's own options, read from Plex rather than copied,
  beside CouchElephant's, and says which of the two will own the rule.
- With no CouchElephant option set, it books a plain Plex rule instead.
- Anything it booked can be cancelled from the same panel.
- Every decision, including the ones it declined, is written down and shown.

### The guide

- Two-axis grid, channels down and time across, both loading as you reach the
  edge.
- Search and a sectioned, searchable filter panel.
- Channel logos, cached and refreshed, with your own artwork allowed to
  override them.

### Recordings

- Schedule reads Plex's own grab list, so it is what will really record.
- Agenda and calendar views over the same data.
- Every entry says who booked it and why. Clicking one opens the programme
  panel with that reason.
- Passes can be paused, edited and removed, and opening one shows what it will
  record next.

### The app

- Light and dark themes, per account when signed in.
- Sign-in off, local accounts, or Cloudflare Access.
- A settings window with sections, sub-tabs and a search that reads all of
  them.
- A first-run screen that tests the Plex connection before saving anything.
- Works on a phone.
