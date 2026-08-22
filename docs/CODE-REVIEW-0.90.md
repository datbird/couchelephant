# Code review, v0.90

**Status: every finding below is fixed.** See the "What was done" section at
the end for how each was verified. The findings are kept as written so the
reasoning survives, not just the diff.

A full read of every Python module and template, done 2026-08-22 against
commit `71e6180`. Each finding says what is wrong, how it was verified, and
what to do. Severity is about consequence, not code smell.

The codebase is ~3,250 lines of Python and ~3,700 of templates. The shape is
sound: one process, one database, the selection logic isolated in `passes.py`,
and the comments say why rather than what. The findings below do not change
that judgement. They are what a careful second reading finds in code that grew
by accretion over two days.

---

## Bugs

### B1. Every sync wipes the sports team tags and re-fetches all of them

`sync.py _upsert_program` writes `teams=excluded.teams` on conflict. A bulk
section listing carries no `Team` array, so every sync overwrites the enriched
tags with `[]`, and `enrich_sports` then re-fetches every sports programme one
at a time. The comment on `enrich_sports` says it "costs a burst on first run
and almost nothing afterwards". That intent is not executed.

Verified: the sync log reads "47 sports enriched" on every sync, five syncs
running. It should read near zero after the first.

Consequence: ~50 extra HTTP requests per sync, forever, and a window during
each sync in which every sports programme briefly has no teams. A pass that
runs in that window sees no games. The pass runner runs after enrichment in
`sync_loop`, so the window only bites someone calling `run_passes` from the UI
mid-sync, but it exists.

Fix: preserve on empty. `teams=CASE WHEN excluded.teams='[]' THEN
programs.teams ELSE excluded.teams END` in the upsert.

### B2. The HD filter excludes 1080 broadcasts

`filters.py`: `("hd", "HD", "a.resolution >= '720'")` compares strings.
`'1080' >= '720'` is false, because `'1'` sorts before `'7'`. Any lineup with
1080 channels has them filtered out of "HD".

Latent here (this lineup stores only `480` and `720`, verified against the
live database), real for most cable lineups. Fix:
`CAST(a.resolution AS INTEGER) >= 720`.

### B3. Uploaded SVG logos can never render

`channel_logo_upload` accepts SVG and stores `custom-X.svg`, but `/logo/{vcn}`
serves every file as `image/png`. An SVG served as PNG draws nothing, so the
feature accepts a file it cannot show.

Do not fix it by serving `image/svg+xml`: an SVG is a document that can carry
script, and serving user uploads as SVG is an XSS vector. Either reject SVG at
upload, or rasterise it there. Rejecting is one line and honest.

### B4. `create_recording` does not URL-encode pref values

`plex.py`: `url += f"&prefs%5B{k}%5D={v}"`. Keys and values go into the URL
raw. The values come from the record panel's free-text fields, so a user who
types a space or `&` into "Minutes before start" produces a malformed URL, and
in the worst case injects a second parameter. Fix:
`urllib.parse.quote(str(v), safe="")`.

### B5. The settings the user picks are dropped when a source limit makes a CE rule

`web.py api_record`: when a recurring template plus a source limit routes to
`_make_ce_rule`, the `settings` the user just filled in are not passed and not
stored. `api_rule_create` (the add panel) stores them in `passes.prefs`;
`_make_ce_rule` (the guide's record panel) silently loses them. The user set
padding and quality and the pass will book without them.

Fix: thread `prefs` into `_make_ce_rule` and store it, as `api_rule_create`
does.

### B6. Matching recordings by title is wrong for any repeated title

Three places treat a programme *title* as an identity:

- `passes.already_handled`: "Plex already has it" if any grab shares the title
- `api_program`: the `scheduled` field, same match
- `api_grid`: `sched="plex"` for any airing whose title is in the grab list

A daily programme titled the same every day ("Aging Untold", local news) makes
every future airing read as already covered, and a pass will decline to book
game N because game N-1's grab has the same title only when titles collide,
which for sports ("Chiefs at Bills") is rare but for series rules is the
normal case. `plex_grabs` rows carry `channel_vcn` and `begins_at`; match on
those where a specific broadcast is meant, and on `(title, begins_at)` where
"this episode is covered" is meant.

### B7. `_test_plex` reads any "401" in the error text as a rejected token

`if "401" in text` matches the port in `http://host:40123` or a 401 embedded
in an HTML error page. Cheap fix: have `PlexError` carry the status code as an
attribute and check that.

### B8. `welcome_save` tests credentials by writing them globally first

The first-run save sets `plex_url`/`plex_token`, runs the test, and reverts on
failure. The background `sync_loop` can fire between the write and the revert
and sync against half-entered credentials. Narrow window, self-healing, but
the pattern is wrong: `_test_plex` should take the candidate values as
arguments instead of reading settings.

### B9. `subscription_exists` can turn a slow Plex into a false failure

`_schedule` checks the new subscription immediately after creating it. A
transient network error in that check raises and the whole booking is reported
failed, though it succeeded. Treat a network error there as "unknown", not
"discarded": only the definite 404 proves Plex dropped it.

---

## Intent not executed

### I1. Following a team from the programme panel books nothing

`POST /api/pass` (the "Follow Seattle Seahawks" button in the overlay) inserts
the pass and returns. It never runs the passes, so nothing is booked until the
next sync, up to an hour away. Every other creation path runs
`passes.run_passes()` and reports "N airings scheduled". The overlay path
should do the same.

### I2. `/api/pass` and `pass_add` skip the duplicate check inconsistently

`api_rule_create` refuses a duplicate team pass with a clear message.
`pass_add` (dead, see V2) inserts blindly. `api_pass` checks
`WHERE team_id = ?` without `kind='team'`. Consolidate on one creation
function; three implementations of "insert a pass" is how they drifted.

### I3. Editing a pass cannot edit its Plex settings

The edit panel offers only the source limit. `passes.prefs` exists and is
applied on booking, but no UI path changes it after creation. Either expose it
in edit or say in the panel that settings are fixed at creation.

### I4. `api_schedule`'s `more` flag is wrong when filtered

`total` counts all grabs, but `start`/`end` filter the rows. The agenda never
passes a window so it works; the calendar passes one and ignores `more`. If
anything ever pages a windowed query, it will loop. Count with the same WHERE.

---

## Vestigial code

### V1. The `/recordings` route computes five datasets the template no longer reads

`rules`, `grabs`, `upcoming`, `teams`, `actions` are all built server-side and
passed to a template that now renders everything from `/api/schedule` and
`/api/rules`. Verified: no Jinja loop in `recordings.html` references any of
them; the matching identifiers in the file are JavaScript variables.

This is also the **single biggest inefficiency**: `upcoming` runs the full
pass evaluation (a table scan plus JSON parse per airing, per pass) on every
page load, and throws the result away. The route should render the shell and
nothing else.

### V2. Dead route `/passes/add`

Its form was removed from the template two commits ago. No reference remains.
Delete it, and with it the only pass-creation path that skips the duplicate
check.

### V3. The `kindicon` Jinja macro is defined and never called

The page draws icons from the JS `KIND_ICON` map now. Delete the macro.

### V4. `guide()` passes `channels=_channel_list()` to a template that ignores it

The channel dropdown it fed was removed when search merged into the guide.
Delete the query and the parameter.

### V5. `already_handled(program_guid, airing_id)` ignores its second argument

Every caller passes `pick["id"]`; the function never reads it. Either use it
(it would sharpen B6) or drop the parameter.

### V6. `section_search` and `by_team` in `plex.py` have no callers

Written for a search path that was replaced by the local SQLite search. Keep
`teams()`; delete these two.

---

## Inefficiencies

### E1. A new TCP connection per Plex request

`Plex._client()` builds a fresh `httpx.Client` per call. `enrich_sports` makes
~50 metadata calls per sync (made worse by B1), `sync_recordings` one per
subscription, each with its own connection setup. Hold one `httpx.Client` on
the `Plex` instance (or use a `with` block per batch), and connection reuse is
free. `cache_logos` already does this correctly.

### E2. `sync_recordings` is N+1

One request lists subscriptions, then one more per subscription for its
settings. The list response already carries `Setting` on some server versions;
where it does not, fetching details only for keys whose `updated_at` changed
would cut it to nearly zero.

### E3. `current_user` runs twice per request

Once in the auth middleware, once in `page()`. With Cloudflare mode that is
two JWT verifications per page. Stash the result on `request.state` in the
middleware and let `page()` read it.

### E4. `auth._con()` runs the schema script on every call

Every session check opens a new SQLite connection and replays four
`CREATE TABLE IF NOT EXISTS`. Run the schema once at import (the main
database already does) and keep a thread-local connection like `db.connect()`.

### E5. `zoneinfo.available_timezones()` is sorted on every settings render

It walks the tzdata directory each time. Compute once at module load.

### E6. `api_record` builds a second `Plex` object to sync with

The one it just used is still in scope. Cosmetic, but it re-reads settings and
signals the wrong thing to a reader.

---

## Reinventing the wheel, internally

The external dependencies are right: FastAPI, httpx, Jinja2 and nothing else
is a defensible spine for an app this size. The wheel-reinvention is internal
duplication between templates:

### M1. The multi-select dropdown exists twice

`guide.html` (record panel) and `recordings.html` (add panel) each carry a
full copy of the networks-and-channels dropdown: `multiField`, `renderMulti`,
`placeMenu`, `flip`, the open/close wiring. They have already drifted once
(the guide copy skips autofocus on touch; the recordings copy never got that).
Extract one component into a shared static JS file.

### M2. `esc()` is defined four times

`base.html` twice (overlay and settings scripts), `guide.html`,
`recordings.html`. One definition in a shared file.

### M3. The team/series picker and the source list are fetched by two panels

Both panels hit `/api/teams`, `/api/series`, `/api/sources` with their own
fetch-debounce-render code. Same extraction as M1.

### M4. Inline `<script>` in templates has outgrown the pattern

`base.html` is 1,843 lines, more than half JavaScript. The
server-rendered-plus-plain-JS decision is right, but the JS now deserves
`static/app.js` (theme, tabs, overlay, settings window) and
`static/recording.js` (panels). The templates stay templates; caching improves
as a side effect.

---

## Security notes for the open-source release

### S1. No CSRF protection while sign-in is off

Sign-in off is the default, and every state-changing route accepts a plain
form POST with no origin check. Any web page the user visits can fire
`POST http://<lan-ip>:8710/settings` or `/api/record` from their browser; the
browser will happily send it cross-origin, and with no cookie needed there is
nothing to withhold. `settings_save` would let such a page null the Plex URL.

With sign-in on, `SameSite=Lax` on the session cookie already blocks the
cookie on cross-site POSTs, so the local mode is covered by accident.

Cheap, adequate fix for both: reject state-changing requests whose `Origin`
or `Referer` header names a different host, when either header is present.
That is a middleware of ten lines and no token machinery.

### S2. `_OPEN` is a prefix match

`path.startswith(_OPEN)` lets `/loginanything` and `/welcome-x` through the
auth gate unauthenticated. Nothing sensitive lives on such paths today (they
404), but match on the exact path or `prefix + "/"`.

### S3. `fmt` uses `%-I`, a glibc extension

Fine in the container, breaks on Windows and BSDs. Worth a note in
DEVELOPING.md rather than a change.

---

## What was checked and is sound

Worth recording so the next reviewer does not re-litigate it:

- **The pin** (`oneShot` + `lineupChannel` + `startTimeslot`) is enforced in
  `_schedule` after merging user prefs, so no caller can unpin a pass booking.
- **SQL injection**: every user value goes through parameters; the two
  f-string interpolations (`marks` placeholders, `int(limit)`) are safe by
  construction.
- **Path traversal**: logo filenames are sanitised to `[A-Za-z0-9._-]`, and
  the read side serves only paths stored by the app.
- **Password handling**: scrypt, per-user salt, constant-time compare, hashing
  on unknown-user too, tokens stored hashed. Correct throughout.
- **Cloudflare verification**: signature, audience and issuer all required;
  the bare email header is never read.
- **XSS in the client-rendered HTML**: every interpolation into `innerHTML`
  routes through `esc()`, and attributes are consistently double-quoted,
  which matches what `esc()` escapes. M2 is the risk here: four copies of the
  escaper is four places a regression can start.
- **Airing id stability**: `our_grabs.airing_id` references survived a night
  of syncs (verified live); the id derives from Plex's stable Media id.
- **The migrations list**, WAL mode, thread-local connections, and the
  guide-cache delete-then-rebuild pattern all do what they claim.

## Suggested order of work

1. B1 (sync waste plus a correctness window), one line.
2. B2, B4, B7: one line each.
3. V1 with I1: strip the `/recordings` route to a shell, make `/api/pass` run
   the passes. Removes the biggest waste and the most visible intent gap.
4. B6: switch the three title matches to `(title, begins_at)` or
   `(channel_vcn, begins_at)`.
5. B5, B3, I2, V2 to V6: small deletions and threading.
6. S1: the origin-check middleware, before the repo goes public.
7. M1, M2, M4: extract shared JS. Do this before the next feature that touches
   both panels, not after.


---

# What was done

Every finding fixed, and verified against the live server rather than by
reading the diff back.

## Measured before and after

| | Before | After |
| --- | --- | --- |
| Sports programmes re-enriched per sync (B1) | 47, every sync | 0 |
| Resolutions the HD filter accepts (B2) | 720 only | 720, 1080, 2160 |
| Copies of the source picker (M1) | 2, already drifted | 1 |
| Copies of `esc()` (M2) | 4, two of them different | 1 |
| Datasets built per `/recordings` load (V1) | 5, including a full pass run | 0 |
| Cross-origin POST (S1) | accepted | 403 |
| `/loginanything` past the auth gate (S2) | allowed | 404 |

## Verification notes

**B1** was the one worth measuring. The sync log read "47 sports enriched" on
every sync before, and reads 0 now, with all 47 programmes keeping their team
tags across syncs. The fix preserves the stored value when an incoming listing
carries an empty array, because a bulk listing not carrying teams means "not
asked", not "no teams".

**B2** was proved in isolation before the change went in: with a string
compare, `SELECT ... WHERE resolution >= '720'` returns only `720` out of
`480, 720, 1080, 2160`. With the cast it returns `720, 1080, 2160`.

**B9** turned out to need care. Making `subscription_exists` tri-state without
updating its two call sites would have made the bug worse, because
`not None` is true and every failed check would have reported "Plex discarded
your recording". Both call sites now test `is False`.

**S1** is a same-origin check rather than CSRF tokens. A browser always sends
`Origin` on a cross-site POST, so comparing it to the host the request arrived
on needs no session and no token machinery. A request with neither header
passes, which is curl, and curl was never the threat. Verified: a POST
carrying `Origin: http://evil.example` gets 403, the same origin gets 200, and
a plain curl call still works.

**I3** exposed a smaller problem while being fixed. Saving a pass's Plex
settings also stored `lineupChannel` and `startTimeslot` from the template. A
pass always overrides those with its own pin, so storing them is noise, and a
reader could believe a pass had been limited to one channel when it had not.
They are stripped on save.

**M1 and M2** carried the most regression risk, since both record panels and
the guide grid depend on them. Verified after the extraction: both panels
drive the one shared picker, both show all 117 sources, both flip the verdict
bar correctly, and a full click-through of the guide, the record panel, the
schedule, the calendar, the passes and settings reported zero page errors.

## Left as noted, not changed

**S3**, `%-I` in `strftime`, is a glibc extension. It is correct in the
container this ships in. Recorded in DEVELOPING.md rather than worked around.

## Not regressed

The two live Chiefs recordings were checked after every deploy and are
untouched: both still scheduled on 41.1, both still held by Plex as
subscriptions 48 and 49.
