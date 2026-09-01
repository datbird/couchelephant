# Passes for things that are not in the guide yet

Status: design, not built. Written 2026-08-31.

## The problem

You read that a new series called Gobiligook starts next March. You want to set
up a pass for it today. You cannot, because a pass matches guide rows and the
guide has no idea the show exists.

The same gap shows up for sport. You follow a team in August and want to see
their season. The guide holds the next two weeks of it.

## The constraint that shapes everything

**No source of TV guide data goes further out than about three weeks.** Not free
ones, not paid ones. Schedules Direct is the licensed Gracenote reseller and it
accepts a request for at most 21 days, typically serving 7. Free XMLTV feeds do
7 to 14. Plex is already giving us 12.

This is not a licensing problem to spend money on. In August no broadcaster has
decided what airs on a given channel next March, so there is nothing to sell.

What does exist months ahead is two other kinds of data, and no single source
carries both:

- **Announcements.** A title, a network and a date that is often only a month or
  a year. "Gobiligook, new series, March 2027, ABC."
- **Scheduled games.** Leagues publish full seasons months ahead. Chiefs at
  Broncos, December 14, 3:25 PM, known in May, before any broadcaster is named.

So this feature is not "get a better guide". It is "hold an intention until the
guide catches up".

## Sources

Chosen on what it costs a user to start, because a feature nobody configures is
a feature nobody has.

| Source | Covers | Setup | Key |
|---|---|---|---|
| TVmaze | series, premiere dates, networks | none | none, ever |
| TheSportsDB | league schedules months ahead | none, but thin | see below |
| TMDB | films | register | free, optional |

TVmaze needs no key and no account, so it is always on. That matters more than
raw coverage: it means the feature works the moment the container starts.

**Correction, measured 2026-08-31.** An earlier draft of this spec said the
TheSportsDB free tier fills in a season. It does not. On the public test key
`eventsseason.php` answered five events for the whole NFL and none for the team
asked about, while `eventsnext.php` answered exactly one game. **A subscriber
key is what gives a season.** Without one this source contributes a single
upcoming game per team.

ESPN's public JSON was re-measured at the same time and does return a full
17 game season, free and keyless. It was still rejected, for the reason below:
it answers a plain client and refuses a browser user agent, so using it means
deliberately not looking like a browser. That is someone's bot protection, and
working around it is not a foundation to ship on. The thin honest source wins
over the rich fragile one.

### Rejected, and why

- **The ESPN public JSON.** It works, and it is what everyone reaches for. It
  also returns 403 to a browser user agent and 200 to `Python-urllib`. It answers
  only when you do not look like a browser. That is bot protection we would be
  tiptoeing around, and it will break one day with no warning.
- **TheTVDB.** Either a negotiated licence, which is an application with no
  guarantee, or every user buys an $11.99 a year subscription for a PIN. Sonarr
  avoids this by holding one licence centrally. We cannot borrow it.
- **Sonarr and Radarr.** Excellent when present, and they already hold years of
  future air dates. Most CouchElephant users will not run them. Not a base to
  build on. Worth revisiting as an extra source later.

## What is already here

`FIELDS["title"]` in `smartfilter.py` is free text, so a smart pass saying
`title contains "Gobiligook"` is already expressible, and it already matches the
instant the show reaches the guide.

Everything this spec adds is the part around that. Today such a pass cannot be
told apart from a broken one: you cannot say what you expect, nothing confirms
the title is real or spelled right, and nothing tells you when March comes and
the show never appeared.

## Data model

A new table, rather than provisional rows inside `programs` and `airings`. The
guide tables are read by every query in the app. Mixing invented rows into them
would mean auditing all of those queries, forever, for a flag they could forget.

    CREATE TABLE IF NOT EXISTS expectations (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        pass_id       INTEGER NOT NULL,   -- the pass waiting on this
        source        TEXT NOT NULL,      -- tvmaze | thesportsdb | tmdb
        source_id     TEXT NOT NULL,      -- the id at that source
        title         TEXT NOT NULL,
        subtitle      TEXT,               -- "Chiefs at Broncos", episode title
        network       TEXT,               -- what the source claims, often a streamer
        expected_at   INTEGER,            -- best known start, UTC
        precision     TEXT NOT NULL,      -- time | day | month | year
        matched_guid  TEXT,               -- set when the guide catches up
        matched_at    INTEGER,
        missed_at     INTEGER,            -- set when the date passed with no match
        updated_at    INTEGER,
        UNIQUE (source, source_id, pass_id)
    );

`precision` is the honest part. A league gives a time. TVmaze often gives only a
month. The UI must never render "March 2027" as "March 1 2027 12:00 AM", because
a reader takes an invented time for a real one.

## Behaviour

### Search

Search works exactly as it does now, and returns guide results at the same speed.
A second pass over the external sources runs behind it and appends what the guide
does not have, marked clearly as not yet scheduled. Guide results never wait on a
network call to a third party.

### Creating the pass

Picking an external result creates an ordinary pass plus its `expectations` rows.
For a series that is one row with a soft date. For a team it is the whole
published season, one row per game.

### Promotion

Every sync, each unmatched expectation is looked for in the guide by title and
date. A match sets `matched_guid`, and from that moment the pass behaves like any
other pass and books through the path that already exists.

**Nothing external ever books a recording.** An expectation is an intention. Only
a guide airing can be recorded, because only the guide knows the channel.

### When it does not arrive

If the guide reaches past an expectation date and nothing matched, set `missed_at`
and raise a notice naming the show. A show can slip, so a miss keeps the
expectation and keeps looking. It is a warning, not a deletion.

### In Recordings

Expectations show as plans, drawn differently from bookings, with the source named
and the date shown at its real precision. A promoted one moves into the normal
list.

## The keys notice

A yellow badge leading to Settings, saying which key unlocks what, and why anyone
would bother. **It can be dismissed.**

That is new. `app/db.py` says of the existing notices that a health problem you
can click away is a health problem you forget about. That rule is right, and it
stays. So this is a different class of thing, not a loosening of the old one:

- A new severity, **`tip`**, alongside `bad` and `warn`.
- `notices` gains `dismissed_at INTEGER`.
- **The dismiss route refuses any notice whose severity is not `tip`.** Enforced
  in code and covered by a test, so a later edit cannot quietly make a health
  problem dismissible.
- Badge severity order is `bad`, then `warn`, then `tip`, so a real fault is never
  hidden behind a suggestion.
- Dismissal is permanent for that code. It is a suggestion, and asking twice is
  nagging.

The tip is raised only when a key would actually add something. No TMDB key set
and the user has made a pass for a film, or no TheSportsDB key and a team pass
exists. An install that only follows broadcast series never sees it.

## Testing

- Unit: precision rendering never invents a time it was not given.
- Unit: promotion matches on title and date, and does not match a rerun.
- Unit: a miss keeps the expectation rather than deleting it.
- Unit: **dismissing a `bad` or a `warn` notice is refused.** Mutation-check this
  one. It is the guard that protects the old rule.
- Live HTTP: search returns guide results without waiting on a third party.
- Browser: an expectation draws as a plan and not as a booking, and a
  month-precision date shows as a month.
- Every provider is faked in tests. No suite reaches a third party.

## Out of scope

- Sonarr and Radarr as sources.
- Anything that books from external data.
- Making the Plex guide reach further. That is not possible.
