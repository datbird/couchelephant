# Changelog

## Unreleased

### Added

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
