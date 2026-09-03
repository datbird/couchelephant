# Outbound alerts, for 1.1

Written 2026-09-03. Supersedes phase 1 of `2026-09-02-chat-integration.md`, which
was written before the four open decisions were answered. That document still
holds phase 2, the inbound bot, and nothing here changes it.

Every decision below is settled. This is a build document, not an options paper.

## Why this exists, from the day it was written

On 2026-09-03 the Plex butler on the live server was found wedged. It had been
wedged for a day, and the same fault had happened twice before in the fortnight.
CouchElephant caught it correctly: `epg_stale` opened on 08-30 and stayed open.

Nobody saw it for four days, because a notice only exists on a page nobody had
open.

That is the whole case for this feature. The detection already works. The
delivery does not exist.

## What is being built

Outbound alerts only. The app says something happened, into Discord or Telegram.
Nothing listens, nothing polls, nothing holds a socket open.

**Discord needs no bot.** A webhook URL is the entire integration. The user makes
one in their own channel settings and pastes it in.

**Telegram needs a bot token but no running bot.** One HTTPS POST to
`api.telegram.org/bot<token>/sendMessage`. "Bot" is only what Telegram calls the
token.

Both are outbound. That matters because a self-hosted install sits behind a
router with no public address, and nothing here may ever need a port forwarded.

## The four decisions

1. **Destinations are a list.** Any number, each named, each with its own event
   selection. Failures to one channel, recordings to another, and a phone
   separately, is a thing people want and a flat pair of settings keys cannot
   express.
2. **Both platforms, one module.** `app/notify.py` holds both transports behind
   one call. Route and sync code never learn which platform is configured.
3. **The catalog is faults plus activity.** All eight health notices, plus four
   derived events. Activity needs new change detection, and doing it later would
   mean touching the same code twice.
4. **A fault reports its whole life.** One message when it opens, a reminder
   while it stays open, one message when it clears.

## The event catalog

Faults come from `health.open_notices()`, which already carries code, severity,
title, detail, `first_seen` and `resolved_at`. Nothing new is needed to produce
them.

| code | what it means |
|---|---|
| `epg_refresh_off` | Plex's own guide refresh is switched off |
| `epg_stale` | Plex has not refreshed its guide |
| `guide_short` | the guide is running out |
| `plex_unreachable` | Plex did not answer |
| `team_pass_unmatched` | a team you follow has no games in the guide |
| `booking_drift` | a booking no longer matches the pass that made it |
| `booking_repair_failed` | that drift could not be repaired |
| `expectation_missed` | the guide passed a date without ever carrying the show |

Activity events are new and derived each sync:

| code | source |
|---|---|
| `pass_booked` | a new `our_grabs` row |
| `recording_started` | a `plex_grabs` row whose `begins_at` has passed |
| `recording_finished` | a started grab that reached a terminal status or vanished |
| `sync_failed` | a `sync_log` row with `ok = 0` |

`keys_available` is `tip` severity and is **excluded from the catalog entirely**,
not merely unticked by default. A tip is a suggestion, the product already treats
it as the only dismissible kind, and a suggestion is never worth a notification.

### Detecting a recording without guessing Plex's vocabulary

Checked against the live server on 2026-09-03: every row in `plex_grabs` carried
`status = 'complete'`, and that is the only value with evidence behind it. Plex's
other statuses are undocumented here, so nothing may depend on knowing them.

So:

- **started** is decided by time. The grab exists and its `begins_at` has passed.
  That needs no vocabulary at all.
- **finished** is decided two ways, either of which is enough: the status becomes
  terminal, or the row disappears. `sync_recordings` deletes any grab Plex stopped
  reporting (`DELETE FROM plex_grabs WHERE updated_at < ?`), and Plex drops a
  completed recording from `scheduled()`, so disappearance is the reliable signal
  and the status set is the belt to its braces.

`TERMINAL` is `{"complete", "completed", "error", "failed", "cancelled",
"canceled", "aborted"}`. Anything not in it is treated as still going. Being
wrong about an unknown status costs a late message, never a missed one.

## Data model

Two new tables. Both go in `SCHEMA`, because `CREATE TABLE IF NOT EXISTS` covers
a new install and an existing database alike. `MIGRATIONS` is only for columns.

```sql
CREATE TABLE IF NOT EXISTS destinations (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL,
    kind         TEXT NOT NULL,              -- 'discord' | 'telegram'
    webhook      TEXT,                       -- discord. a secret.
    token        TEXT,                       -- telegram. a secret.
    chat_id      TEXT,                       -- telegram. not a secret.
    events       TEXT NOT NULL DEFAULT '',   -- comma-separated event codes
    remind_hours INTEGER NOT NULL DEFAULT 24,-- 0 disables reminders
    enabled      INTEGER NOT NULL DEFAULT 1,
    created_at   INTEGER,
    updated_at   INTEGER
);

CREATE TABLE IF NOT EXISTS notify_state (
    destination_id INTEGER NOT NULL,
    event          TEXT NOT NULL,
    key            TEXT NOT NULL,   -- notice code, grab id, our_grabs airing id
    opened_at      INTEGER,
    last_sent_at   INTEGER,
    PRIMARY KEY (destination_id, event, key)
);
```

`notify_state` is the whole repeat-suppression mechanism, and it serves both
families with one rule: **a row exists means this destination has been told.**

- A fault keeps its row while open and gets a reminder when
  `now - last_sent_at >= remind_hours`. On clearing, send the recovery message,
  then delete the row.
- An activity event writes its row once and never reminds. The row existing is
  what stops the next hourly sync announcing the same booking again.

Rows for activity events would otherwise grow forever, so `dispatch` prunes any
row older than 30 days whose event is not a fault.

Per-destination state is not optional. Two destinations may hold different
`remind_hours`, and one that is added tomorrow must not be sent a backlog of
everything that opened last week. A new destination starts with no rows, so the
first thing it hears is the next thing that happens.

## The module

`app/notify.py`. One entry point that sync calls, one that the test button calls.

```python
def dispatch(now: int | None = None) -> int      # returns messages sent
def test(dest: dict) -> str                      # a verdict string for the UI
```

Everything else is private. The two transports:

```python
def _send_discord(dest, title, detail, severity) -> None
def _send_telegram(dest, title, detail, severity) -> None
```

Discord gets an embed coloured by severity. Telegram gets plain text. Both take
the same `(title, detail, severity)`, so a third platform later means one new
`_send_*` function and no change anywhere else.

`dispatch` is called at the end of `full_sync`, **after the `sync_log` insert**,
so `sync_failed` can see its own row.

## Safety

**The Discord webhook URL is a bearer credential.** Anyone holding it can post to
the channel. It and the Telegram token both render as `'*' * 12` when set, the
same as `plex_token` in `_settings.html`, and neither may reach a log line.

**The webhook is a user-supplied URL the server will POST to**, which is a
server-side request forgery hole if left open. It is validated against the
Discord host: the scheme must be `https` and the host must be `discord.com`,
`discordapp.com`, `ptb.discord.com` or `canary.discord.com`. Telegram has no
user-supplied host at all, since the URL is built from `api.telegram.org` and the
token.

**A send that fails is logged and swallowed.** A notification that cannot be
delivered must never break a sync. `dispatch` is wrapped at its call site as
well, so an unexpected exception inside it cannot take the sync with it.

**A disabled or misconfigured destination is skipped, not retried.** There is no
queue and no backoff. The next sync is in an hour and it will try again.

## Telegram chat id

The chat id is where people get stuck, so the app finds it. After the token is
saved, a button calls `getUpdates`, and the user is told to message the bot once.
The first update carries the chat id and the app stores it. Nobody hunts for a
numeric id by hand.

If `getUpdates` returns nothing, say so plainly and say what to do: message the
bot, then press it again.

## Settings

A new **Notifications** section in the settings nav, after Recording. One tab,
Destinations.

- A list of destinations, each showing name, kind, and how many events it carries.
- Add, edit, test and remove.
- Inside the editor: name, kind, the credential fields for that kind, the event
  checklist grouped into Faults and Activity, and the reminder interval.
- A **Send a test message** button on each destination. Every integration like
  this needs one, and nobody trusts it without.

The message shape, so both look deliberate rather than dumped:

```
[BAD] Guide has gone stale
Plex refreshes its guide every 1 day. It last refreshed 5 days ago.
Nothing new is reaching the guide, so it gets shorter every day.
```

A reminder prefixes `Still open:` and adds how long it has been open. A recovery
prefixes `Cleared:` and says how long it was open for.

## Testing

`tests/fake_chat.py`, serving the Discord webhook and Telegram Bot API shapes
over real HTTP on localhost, exactly as `tests/fake_sources.py` does for the
providers. **No test may reach Discord or Telegram.** The root conftest already
refuses to run outside scratch and the same rule applies to the network.

Pinned:

- a notice opening sends once, and a second sync with the same notice open sends
  nothing
- a reminder fires once `remind_hours` has passed and not before
- a notice clearing sends a recovery message and deletes its state row
- a `tip` never sends, and `keys_available` is not in the catalog
- two destinations with different event lists each receive only their own
- an activity event fires once per key, however many syncs run
- a destination added today is not sent a backlog of what is already open
- a send that fails does not break the sync
- neither the webhook nor the token appears in a rendered page or a log line
- a webhook URL pointing anywhere but Discord is refused
