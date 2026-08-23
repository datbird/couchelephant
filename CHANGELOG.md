# Changelog

## Unreleased

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

### Changed

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
