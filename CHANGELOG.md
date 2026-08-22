# Changelog

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
