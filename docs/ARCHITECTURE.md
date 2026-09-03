# Architecture

One container, one process, one SQLite file. Python 3.12, FastAPI, Jinja2 and
httpx. No build step, no client framework, no message queue.

The pages are server rendered and the interactive parts are plain JavaScript in
the templates. That is a deliberate choice: this is a small app that has to be
readable by whoever inherits it.

## Modules

| File | What it owns |
| --- | --- |
| `app/db.py` | The schema, migrations, settings, and the two query helpers |
| `app/plex.py` | The Plex client. Every request to Plex goes through here |
| `app/sync.py` | Pulling the guide and Plex's recordings into SQLite, and caching logos |
| `app/passes.py` | **Choosing which airing to record.** The heart of the app |
| `app/smartfilter.py` | The smart filter: a nested condition tree, compiled to SQL |
| `app/health.py` | **Watching Plex do its own job**, and the notices that come of it |
| `app/notify.py` | **Sending those notices somewhere you will see them.** Discord, Telegram and Notifiarr, behind one call |
| `app/verify.py` | **Comparing a booking against the pass that made it**, without inventing a difference |
| `app/teamcat.py` | The shipped team catalogue, and matching a Plex team to it |
| `app/filters.py` | The guide's filter tokens |
| `app/auth.py` | Accounts, sessions and per-user preferences |
| `app/cf_access.py` | Verifying Cloudflare Access tokens |
| `app/dbstore.py` | What counts as durable data, and the three-way merge |
| `app/portable.py` | Export and import, as one zip |
| `app/backups.py` | Snapshot jobs: schedule, retention, encryption, restore |
| `app/backingstore.py` | The two-way replica, and its database backends |
| `app/web.py` | The FastAPI app: middleware, the background loops, and mounting the routes |
| `app/routes/_shared.py` | What every route needs: the template engine, `page()`, who is asking, the Plex client |
| `app/routes/guide.py` | The grid, a programme, search, channel logos |
| `app/routes/record.py` | Recording one broadcast, Plex's templates and settings, making a pass |
| `app/routes/passes.py` | The schedule, passes, rules, smart filters |
| `app/routes/account.py` | First run, sign in, sign out, theme |
| `app/routes/data.py` | Export, import, backups, the backing store |
| `app/routes/settings.py` | The Plex connection, accounts, artwork, sync, alert destinations |

`dbstore.py`, `portable.py`, `backups.py` and `backingstore.py` are explained
in [Your data](DATA.md), including why the guide is never copied.

`passes.py` is where the value is. If you read one file, read that one.

## The database

Two files, on purpose. The guide cache can be rebuilt from Plex at any time;
accounts cannot. Keeping them apart means a destructive fix to one can never
take the other with it.

### `couchelephant.db`

| Table | What it is |
| --- | --- |
| `settings` | Key and value. Everything configurable |
| `channels` | One row per channel, with its logo and network |
| `programs` | One row per programme, with genres and team tags |
| `airings` | **One row per broadcast.** The same game appears several times |
| `teams` | Every team seen in the guide, kept after it stops playing. `in_guide` says which are on this week. **The id is not stable, see below** |
| `passes` | Our rules: a team, a programme or a smart filter, with an optional source limit. `uid` names one on any machine; `id` does not |
| `pass_actions` | Every decision a pass made, including the ones it declined. Trimmed to 60 days |
| `our_grabs` | Broadcasts we booked, and what booked them. Trimmed to 60 days |
| `plex_subscriptions` | Mirror of Plex's own rules |
| `plex_grabs` | Mirror of Plex's own scheduled recordings |
| `sync_log` | One row per sync, with what Plex's guide looked like at the time |
| `notices` | Something wrong that you need to know about. One row per condition |
| `destinations` | Where alerts go. One row per channel, each with its own list of events. `uid` names one on any machine; `id` does not |
| `notify_state` | What each destination has already been told. **A row existing is the whole repeat-suppression mechanism** |
| `sync_shadow` | What the backing store and this database agreed on last time |
| `backup_jobs` | Snapshot jobs, and how each one last went |

The distinction between `programs` and `airings` is the whole project. A
programme is a thing; an airing is one broadcast of it, on one channel, at one
time. Plex conflates the two when it schedules, and that is the bug.

### `auth.db`

`users`, `sessions`, `email_map` and `prefs`. Passwords are scrypt hashed with
a per-user salt. Session tokens are stored hashed, so a copy of the file grants
no logins.

## Migrations

`db.py` holds a `MIGRATIONS` list of `(table, column, declaration)`. On start,
any column not already present is added.

`CREATE TABLE IF NOT EXISTS` does nothing to a table that already exists, so a
new column in the schema string alone would never appear on an existing
install. The list is the mechanism, not a convenience.

## The sync loop

A background task started at boot:

1. Discover the EPG provider and its Shows, Sports and Movies sections. These
   are per DVR and per server, so they are discovered rather than configured.
2. Pull channels, then each section's programmes and airings.
3. Enrich sports programmes one at a time to get their team tags. A bulk
   listing returns genres but **not** teams; only the per-programme metadata
   has them. Most sport in a guide is not a game, and Plex has no teams for a
   phone-in or a highlights show, so every attempt is dated in
   `programs.teams_tried_at`. A row that came back empty is asked again a day
   later, not an hour later, and never written off: a game can reach the guide
   before Plex tags it.
4. Cache any channel logo that is missing, changed, or older than 30 days.
5. Mirror Plex's own subscriptions and scheduled recordings.
6. Trim pass history older than 60 days.
7. Check that Plex is keeping its own guide up to date, and raise or clear the
   notices that follow. See [Health notices](#health-notices).
8. Check that every recording a pass booked still matches what the pass says,
   and put right the ones that do not. See [Bookings drift](#bookings-drift).
9. Run every enabled pass.

Then sleep for the configured interval. The sync icon in the header runs one
immediately.

## Nothing here is regional

Networks, genres, content ratings and channel lists all come from your own
guide with `SELECT DISTINCT`, never from a list shipped in this repository. The
network of a channel is whatever Plex puts in parentheses, so `(NBC)` and
`(BBC One)` parse alike.

A recording template is identified by Plex's `type` (4 is one broadcast; 2 is a
series or league; 15 is a team), never by the words in its title. Plex
localizes those titles, and reading the English out of them broke the app's one
job for anyone whose server is not in English.

Times, dates, day names and the first day of the week are formatted by the
browser from the viewer's own locale. Where the server has to render a time it
writes a 24-hour form and marks it with `<time data-ts>` for the page to
correct.

Text matching folds case through `ulower()`, a Unicode-aware `lower()`
registered on every connection, because SQLite's own folding stops at Z.

## Rendering

`base.html` holds the header, the tab bar, and the shared programme panel. The
stylesheet is `static/css/app.css` and the shared page behaviour is
`static/js/app.js`; both are versioned by `asset_v`. Every colour in the app
comes from one token block at the top of the stylesheet, so a theme is a swap
of that block and nothing else.

The programme panel lives in `base.html` rather than on the guide, because the
guide, the schedule, the calendar and a pass all open the same thing.

`_settings.html` renders the settings window, and is used both by the overlay
the gear opens and by `/settings` as a whole page. One file, so the two cannot
drift.


## Health notices

CouchElephant can only choose from the airings Plex offers it. When Plex stops
refreshing its guide nothing here breaks: passes keep running, syncs keep
succeeding, and the guide gets a day shorter every day until the game you
wanted is past the end of it. Nothing errors. You find out the evening the
recording does not happen.

So `health.py` watches Plex rather than watching us, and it asks Plex what it
intends rather than assuming:

- `GET /butler` gives the real interval of Plex's guide refresh, and whether it
  is switched on at all. **That response is not wrapped in a MediaContainer**,
  unlike everything else Plex serves. Unwrapping it the usual way returns an
  empty dict, which reads as "Plex has no scheduled tasks" and is a lie a check
  would act on. `Plex._get_root` exists for that one endpoint.
- `GET /livetv/dvrs` gives `refreshedAt`, when Plex last actually did it.
- The furthest airing we hold is the consequence: it is what runs out.

Four conditions raise a notice. The refresh task being switched off, the guide
not having refreshed within twice its own interval, the guide reaching less
than three days ahead, and Plex not being reachable at all. A daily task that
slipped one day is late rather than broken, and complaining about it would
train you to ignore the badge.

Both readings are written onto every `sync_log` row, so a guide that has
stopped moving is visible the next day rather than the week after.

A notice is raised by a check and cleared by the same check passing. There is
no dismiss, by design: a health warning you can click away is one you will
click away. `first_seen` survives every re-raise, because the question you ask
on finding a stale guide is "since when", and a problem that comes back after
being fixed starts its age again.

It shows as a badge on the sync button rather than a fourth icon in the header.
A guide that has stopped moving is a sync problem, and it belongs on the
control you would reach for anyway. The badge is its own button: reading what
is wrong must not start a sync.


## Bookings drift

A pass books a game once and then stops looking at it. `passes.already_handled`
asks "did we book this game" and never "is that booking still right", which is
correct for booking and wrong for everything after it.

That cost a real recording here. A team pass booked a game, the pass's settings
were changed two hours later, and the booking kept the settings it was made
with. Nothing errored. The pass ran, the recording existed, and the game was cut
off at the scheduled end.

So every sync re-reads each future booking from Plex and compares it against the
pass as it stands now. Four things have to be true, and the last two are the
ones a settings check would miss:

1. Plex still holds the subscription.
2. Plex has actually scheduled a recording against it. A subscription whose
   settings all read correctly, with no grab behind it, looks healthy from every
   angle except the one that matters.
3. Every setting the pass carries matches Plex's copy.
4. The pin still names the airing the pass chose. The guide can move a game
   after it was booked, and a stale pin hands the choice back to Plex.

A real difference is repaired: cancel, then book again from what the pass says
now. Delete before create, because creating first would leave two subscriptions
if the delete then failed and Plex would record the game twice.

### Refusing to invent a difference

This is the hard half, and `verify.same` is where it lives. Plex answers
`oneShot` as the string `true` to the `1` we sent. It returns numbers as strings
on one payload and ints on another. It omits a setting rather than reporting it
empty.

A comparison that read any of those as drift would cancel and re-book the same
recording on every sync, for ever, against a live DVR. So values are compared as
numbers first, then as booleans, then as text, and a setting Plex did not report
is `unchecked` rather than different. Not knowing is not the same as
disagreeing, and only the second is grounds for cancelling a recording.

The same rule covers Plex being unreachable. `Plex.subscription_state` answers
`gone` only for a definite 404; a timeout or a 500 is `unknown`, and nothing is
touched. Reading a network blip as "the recording is gone" would cancel and
re-book every booking on the server at once.

### When it will not repair

Repair has a moment in the middle with nothing scheduled, so it needs room. It
runs only when the broadcast is more than two sync intervals away, and never
inside half an hour whatever the interval, so a re-book that fails still has a
later sync to put it right. Drift found closer than that raises a notice instead
of being fixed: wrong padding on a game you are recording beats no recording of
it.

A repair that worked is not a notice. It is written to `pass_actions` as a
`repaired` row carrying what changed, and counted in the sync line. Only what is
still wrong belongs on the badge.


## Team ids are not stable

Plex renumbers its team ids every time it refreshes the guide. Measured on a
live server: one refresh moved the Kansas City Chiefs from 236 to 245 and the
Seattle Seahawks from 132 to 244, on the same game, with the same programme
guid.

**The name is the identity. The id is a handle into whatever guide Plex is
holding right now.** The original design had this backwards, and a pass that
followed the old number silently matched nothing from then on. Matching nothing
is not an error. It looks exactly like a team with no games this week.

Two things follow, and both are needed:

- `resolve_team_passes` runs on every sync, not only when the id is NULL, and
  repoints a pass at whatever id Plex uses today. Only teams currently
  `in_guide` may win it: after a renumber the old row and the new one both sit
  in `teams` under the same name, and picking whichever came back last would be
  a coin toss.
- `candidate_airings` matches on the id **or** the name. This is not belt and
  braces. `programs.teams` is enriched once and then preserved, so a cached
  programme keeps the old ids long after Plex has moved on. Repointing the pass
  to the new id while the cache still holds the old one finds *nothing*:
  verified live, a pass repointed to 245 matched 0 airings against a cache
  still saying 236. Each half covers the other's gap.

### Two folds, and only one of them may pick a recording

`teamcat.norm` drops club words, so "Real Madrid" and "Atletico Madrid" both
fold to "madrid". Five pairs in the shipped catalogue do the same, among them
Cincinnati and FC Cincinnati. That is right for *finding* a team and wrong for
deciding what to record.

So there are two:

- `teamcat.norm` folds identity. Used once, to find a team in the catalogue and
  to recognise it when Plex first carries it.
- `teamcat.ident` folds only spelling: case, accents, punctuation. Every word
  is kept. This is the one `candidate_airings` uses, and `db.connect` registers
  it as the SQLite function `tident`.

Both share `_fold`, and two details in it are the difference between working
outside Latin script and not:

- The separator pass keeps anything **Unicode** calls alphanumeric, not
  `a-z0-9`. An `[^a-z0-9]` filter does not narrow a Cyrillic, Greek, Japanese,
  Hebrew or Arabic name, it deletes it. Every such team then arrived at the
  same empty key, and an empty key is not a miss.
- A combining mark comes off **only when the letter it sits on is ASCII**.
  Beyond Latin a mark carries meaning: the Japanese dakuten is the difference
  between KA and GA, so stripping it turns the Hanshin Tigers into a word that
  is not "tigers". Hebrew niqqud and Arabic harakat are the same mistake. What
  survives is recomposed with NFC, because a bare combining mark is not
  alphanumeric and the separator pass would drop it straight back out.

Every lookup keyed on a fold refuses an empty key rather than looking it up.
`teamcat.find` used to answer VfB Stuttgart for any name written in Cyrillic or
Japanese, because the alias "VFB" folds to nothing once club words are stripped
and so claimed that key. That is the fail-open shape: a miss that comes back
looking like a hit.

The gap between the two spellings is closed at the source rather than papered
over at the match. When `resolve_team_passes` repoints a pass it also adopts
Plex's own spelling of the team, so a pass made from the catalogue stops
carrying a name the guide has never used. After that the pass and the guide
agree word for word.

Because the failure is silent by nature, `check_team_passes` raises a notice
when an enabled team pass can find no game anywhere in the guide. A team out of
season trips it too, which is the right trade: being told a pass is idle is
cheap, and finding out in October that it has been idle since August is not.

## Looking past the end of the guide

`app/sources/` holds the outside providers: `tvmaze.py` (announced series, no
key), `thesportsdb.py` (published league schedules) and `tmdb.py` (films,
optional key). They return `Announcement` records and never book anything.

`app/expectations.py` is what a pass is still waiting for. `store` writes them,
`promote` binds one to a real guide airing once it appears, `sweep_misses`
reports a date the guide reached past without a match, and `render_when` shows
a date at exactly the precision the source gave it.

The `expectations` table is deliberately separate from `programs` and
`airings`. Those are read by every query in the app, and invented rows in them
would mean auditing all of those queries, forever, for a flag they could forget.
