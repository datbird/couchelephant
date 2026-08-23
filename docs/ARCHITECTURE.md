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
| `app/teamcat.py` | The shipped team catalogue, and matching a Plex team to it |
| `app/filters.py` | The guide's filter tokens |
| `app/auth.py` | Accounts, sessions and per-user preferences |
| `app/cf_access.py` | Verifying Cloudflare Access tokens |
| `app/dbstore.py` | What counts as durable data, and the three-way merge |
| `app/portable.py` | Export and import, as one zip |
| `app/backups.py` | Snapshot jobs: schedule, retention, encryption, restore |
| `app/backingstore.py` | The two-way replica, and its database backends |
| `app/web.py` | Routes, and the HTML they render |

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
| `teams` | Every team seen in the guide, kept after it stops playing. `in_guide` says which are on this week |
| `passes` | Our rules: a team, a programme or a smart filter, with an optional source limit. `uid` names one on any machine; `id` does not |
| `pass_actions` | Every decision a pass made, including the ones it declined. Trimmed to 60 days |
| `our_grabs` | Broadcasts we booked, and what booked them. Trimmed to 60 days |
| `plex_subscriptions` | Mirror of Plex's own rules |
| `plex_grabs` | Mirror of Plex's own scheduled recordings |
| `sync_log` | One row per sync |
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
   has them.
4. Cache any channel logo that is missing, changed, or older than 30 days.
5. Mirror Plex's own subscriptions and scheduled recordings.
6. Run every enabled pass.

Then sleep for the configured interval. The sync icon in the header runs one
immediately.

## Rendering

`base.html` holds the theme tokens, the header, the tab bar, and the shared
programme panel. Every colour in the app comes from one token block, so a theme
is a swap of that block and nothing else.

The programme panel lives in `base.html` rather than on the guide, because the
guide, the schedule, the calendar and a pass all open the same thing.

`_settings.html` renders the settings window, and is used both by the overlay
the gear opens and by `/settings` as a whole page. One file, so the two cannot
drift.
