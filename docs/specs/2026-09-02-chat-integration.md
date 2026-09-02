# Discord and Telegram, for 1.1

Written 2026-09-02 to open the next session. Nothing here is built. There is no
notification surface in the app at all today: `grep -riE "webhook|notify|discord|telegram" app/`
returns nothing, so this is greenfield rather than an extension.

## What was asked for

> "add discord and telegram integration. This would just be adding alert
> notifications or bot integration to ask about recordings/notifications etc."

Two halves, and they are not the same size of job:

1. **Outbound alerts.** The app says something happened. Easy, and worth doing first.
2. **Inbound bot.** A person asks the app a question and gets an answer. Harder,
   and it drags authentication in with it.

Ship 1 first. It is useful on its own and 2 is meaningless without it.

## The constraint that shapes the whole design

**A self-hosted install has no public address.** CouchElephant runs on somebody's
LAN behind a router. That rules out the normal inbound path for both platforms:

| | outbound | inbound |
|---|---|---|
| Discord | webhook URL, no bot, no gateway | needs a bot on a gateway websocket |
| Telegram | Bot API `sendMessage`, token only | webhook needs a public HTTPS URL, **or** long polling |

So inbound has to be **the app dialling out and holding a connection**, never the
platform calling in. Discord means a gateway websocket. Telegram means
`getUpdates` long polling. Both are outbound connections, which is what a home
server can actually do. Do not design anything that needs a port forwarded.

## Phase 1: outbound alerts

### Discord

A **webhook URL** is the whole integration. No bot, no application, no OAuth, no
gateway. The user creates one in their own server's channel settings and pastes
it in. `POST` a JSON body and it appears in the channel.

This is a much smaller commitment than a bot and covers everything phase 1 needs.
Do not create a Discord application for phase 1.

### Telegram

A **bot token** from BotFather, plus a **chat id**. `POST` to
`api.telegram.org/bot<token>/sendMessage`.

The chat id is the awkward part of setup, and it is where users get stuck. The
app should find it for them: after the token is saved, call `getUpdates`, tell
the user to message the bot once, and read the chat id off the first update.
Never make somebody hunt for a numeric id by hand.

### What to actually send

The app already has the right event source. `app/health.py` records notices with
a code, a severity (`tip` / `warn` / `bad`) and a title, and `health.open_notices()`
lists what is open. **Send on a notice opening, not on every sync**, or a healthy
server sends an identical message every hour and the user mutes the channel.

Worth an alert:

- a booking failed or could not be repaired (`BOOKING_REPAIR_FAILED`)
- the guide has gone stale (`EPG_STALE`), which means recordings will start missing
- a pass matched nothing when it should have
- an expectation the guide passed without ever carrying (`sweep_misses`)
- a recording actually started or completed, if the user wants that

Not worth an alert, and the reason matters: **anything the user can already see
by looking.** A notification that repeats the UI is noise, and noise gets muted,
and a muted channel silently drops the one alert that mattered.

`tip` severity must never alert. It is a suggestion, and the product already
treats it as the only dismissible kind.

### Settings and shape

- One new module, `app/notify.py`, holding both transports behind one
  `send(title, detail, severity)` call. Route code must not know which platform
  is configured, or adding a third means touching every caller.
- Config in `settings`, same as every other key: `discord_webhook`,
  `telegram_token`, `telegram_chat_id`, plus per-severity toggles.
- **The token is a secret.** Mask it in the Settings UI the way `tmdb_key` is
  masked (`"*" * 12`), and never log it. See `_settings.html`.
- A "Send a test message" button. Every integration like this needs one, and
  users will not trust it without.
- Failure is never fatal. A notification that cannot be delivered must not break
  a sync. Wrap the send, log, carry on. `sweep_misses` and the health pass run
  inside `full_sync`.

## Phase 2: the inbound bot

Only after phase 1 is shipped and used.

### The security problem, first

An inbound bot answers questions about somebody's home DVR. **Anyone who finds
the bot can message it.** So before any command works there must be an allowlist:

- Telegram: a set of permitted chat ids. Store them; refuse everything else,
  silently.
- Discord: a set of permitted user ids, and probably a single permitted channel.

Refuse by default and make the user add themselves. Do not ship a bot that
answers a stranger.

### What it should answer

Keep the command set small and read-only at first:

- `what is recording tonight` / `next` -> the next few from `_schedule_rows`
- `waiting` -> what `expectations.waiting()` holds
- `health` -> `health.open_notices()`
- `status` -> last sync, guide horizon, counts

**Read-only to begin with.** A chat message that cancels a recording is a much
bigger trust decision than one that reports on it, and it should be its own
conversation.

### Mechanics

- Telegram: `getUpdates` long poll in a background thread, same shape as the
  existing sync loop. Respects `COUCHELEPHANT_NO_SYNC_LOOP` style gating so tests
  never open a socket.
- Discord: the gateway is a websocket and a heartbeat protocol. It is
  substantially more work than Telegram. **Consider shipping Telegram inbound
  first** and Discord inbound later, rather than holding both back.

## Testing

Follow what is already there. `tests/fake_sources.py` serves the outside world
over real HTTP on localhost, so the provider modules are exercised through httpx
rather than mocked. Do the same here: a `tests/fake_chat.py` answering the
Discord webhook and Telegram Bot API shapes, including their failure modes.

**No test may reach Discord or Telegram.** The root conftest already refuses to
run outside scratch, and the same rule applies to the network.

Pin at least:

- a notice opening sends once, and the same notice open again does not
- a `tip` never sends
- a send that fails does not break the sync
- the token never appears in a rendered page or a log line
- an inbound message from an id not on the allowlist is refused

## Open decisions for the user

1. **Which events do you actually want?** The list above is a starting point.
   Too many and the channel gets muted.
2. **One channel or per-severity routing?** Simplest is one. Some people want
   failures separate from routine.
3. **Inbound at all, or alerts only?** Alerts cover most of the value. The bot is
   most of the work.
4. **If inbound: read-only, or should it be able to cancel and book?** This is a
   trust decision, not a technical one.

## Suggested opening prompt for next session

> Build phase 1 of `docs/specs/2026-09-02-chat-integration.md`: outbound alerts
> to Discord and Telegram, driven off health notices, with a test button and
> masked secrets. Read the spec first, then confirm which events should alert.
