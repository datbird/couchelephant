# Developing

## Run it locally

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
COUCHELEPHANT_DB=./dev.db \
COUCHELEPHANT_LOGOS=./dev-logos \
uvicorn app.web:app --reload --port 8710
```

Then open `http://127.0.0.1:8710` and point it at a Plex server.

## Deploying

`deploy.sh` ships the source to a host, builds the image there, and **recreates**
the container.

```bash
cp .env.example .env      # then fill in COUCHELEPHANT_HOST
./deploy.sh
```

`.env` is git ignored. Nothing about one person's network belongs in the repo,
so `deploy.sh` has no default host.

It recreates rather than restarts on purpose. `docker restart` keeps a
container pinned to the image it was created with, so a rebuilt image is
silently ignored and you spend twenty minutes debugging code that is not
running.

## Conventions

**Comments say why, not what.** The code already says what it does. A comment
earns its place by recording a decision, a constraint, or a trap that cost
someone an afternoon.

**Errors are sentences.** Every message a user can see should say what happened
and what to do, in plain words. `PlexError: 400` is not a message.

**Never report success you have not checked.** Plex will answer 200 and then
discard what it just made. Read it back.

**One source for a colour.** Every colour comes from the token block at the top
of `base.html`. If you are typing a hex value anywhere else, you are making the
next theme harder.

**Two colours mean two things.** Amber is CouchElephant, blue-grey is Plex,
everywhere, on every page.

## Things worth knowing before you change them

`passes.choose_airing` is the product. Changing how it picks changes what the
app is for.

The pin, `oneShot` with `lineupChannel` and `startTimeslot`, is the mechanism.
Without it Plex re-chooses and records the repeat.

The distinction between `programs` and `airings` is not bookkeeping. A
programme is a thing; an airing is one broadcast of it. Collapse them and the
bug comes back.

The `MIGRATIONS` list in `db.py` is how a column reaches an existing install.
Adding it to the schema string alone does nothing, because
`CREATE TABLE IF NOT EXISTS` skips a table that exists.

A Jinja template's `{% block title %}` ends with the same `{% endblock %}` the
body does. A careless replace over the whole file puts your script in the
`<title>`, where it never runs and takes an hour to notice.

## The scripts are versioned, and they have to be

`base.html` asks for each script as `/static/js/ce.js?v={{ asset_v }}`, where
`asset_v` is the version plus the newest modification time under `app/`. Drop
the query and a deploy stops reaching the browser: the server has the new file,
the browser keeps yesterday's, and a fix you have shipped and tested looks like
it was never made. That is a real afternoon, spent.

`test_conventions.py` fails if a script is asked for without one.

## Known platform assumptions

`fmt()` uses `%-I` in its strftime pattern, a glibc extension for an unpadded
hour. It is correct in the container this ships in, and breaks on Windows and
the BSDs. If you run the app outside Linux, that is the line to change.

## The test suite

```bash
./scripts/test.sh              # everything
./scripts/test.sh --unit       # no browser
./scripts/test.sh --ui         # only the browser checks
./scripts/test.sh --unit -k passes -x
```

It runs in a throwaway container built from `Dockerfile.test`. The working tree
is mounted read only and copied into the container's own scratch, so a run
checks what you have edited and cannot write back over it.

That image is based on Playwright's, not on the slim base the app ships on.
`playwright install --with-deps` asks apt for `ttf-unifont` and
`ttf-ubuntu-font-family`, which no longer exist in Debian, so building the
browser onto the shipping base fails. Microsoft's image already carries
Chromium and its libraries, pinned to the same Playwright version as
`requirements-dev.txt`. The app is pure Python with pinned dependencies, so it
behaves the same either way.

### It refuses to run anywhere real

`tests/isolation.py` is checked before a single test is collected. It rejects
any database or logo path under `/data`, `/config`, `/mnt` or `/var/lib`, any
path that does not name itself scratch, any path left unset, and any Plex
address that is not on localhost.

This is not caution for its own sake. The same pattern without this guard, in
another project by the same author, inherited a live data directory from the
container it ran in and deleted 66,000 rows. A suite that can reach production
will eventually reach production.

### There is no real Plex in it

`tests/fake_plex.py` is a small HTTP server that answers like a Plex Media
Server, including the parts that are wrong: a bulk listing that carries genres
but not teams, a guid that is answered 400 when it arrives encoded twice, a
create that returns a key and then discards the subscription, `oneShot` coming
back as the string `'true'`, `mediaIndex` as a string.

Those quirks are the point. They are each a debugging round that happened once,
and the client is exercised through httpx against a real socket rather than
mocked, so a regression in `plex.py` is caught here instead of in front of a
DVR.

Its guide is anchored just ahead of the current time, because the app's own
horizons are relative: passes look thirty days ahead and the grid draws around
now. `COUCHELEPHANT_FAKE_ANCHOR` overrides the anchor.

### Both clocks are pinned to UTC

The guide is rendered from the server's configured timezone and positioned by
the browser's clock. When those disagree the grid asks for a window a day away
from the data and draws nothing. So the suite sets the app's timezone to UTC
and gives every browser context `timezone_id="UTC"`.

That is a real limitation of the page, not only of the test. It shows up when
the machine running the browser is in a different zone from the one configured
in Settings, and the guide happens to straddle midnight in one of them. Worth
fixing properly one day; noted here so the next person does not spend an
evening on it, as this one did.

### What is covered

| | |
| --- | --- |
| `test_isolation.py` | that the guard above actually refuses |
| `test_plex_client.py` | the client, against the fake server's quirks |
| `test_passes.py` | choosing the airing, source limits, the pin |
| `test_sync.py` | the pull, enrichment, attribution |
| `test_filters.py` | guide filter tokens |
| `test_smartfilter.py` | the smart filter: what it compiles to, and what it refuses |
| `test_teamcat.py` | the shipped team catalogue, and how it meets Plex's list |
| `test_auth.py` | hashing, sessions, the three modes |
| `test_api.py` | every endpoint, in process |
| `test_dbstore.py` | the three-way merge, and export/import |
| `test_backingstore.py` | the two-way store, against a real SQLite file |
| `test_backups.py` | snapshot jobs, retention, encryption, restore |
| `test_conventions.py` | rules the code holds itself to, checked not remembered |
| `ui/` | the browser: guide, record, recordings, smart passes, backup and restore, settings, phone, first run |

The UI suite fails a test on any uncaught page error, so an exception that
leaves a panel half drawn is a failure even when the assertions would pass.

### Running it without Docker

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
playwright install chromium
pytest --ignore=tests/ui        # unit and API
pytest tests/ui                 # browser
```

Playwright is optional: without it the `ui/` tests skip with a reason rather
than failing.

## Testing against a real server

The suite covers the app. It cannot cover your server, and the interesting
failures are all in what a particular Plex returns. If you test against your
own, remember that creating a recording is real. Clean up what you make.
