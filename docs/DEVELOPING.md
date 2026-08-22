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

## Known platform assumptions

`fmt()` uses `%-I` in its strftime pattern, a glibc extension for an unpadded
hour. It is correct in the container this ships in, and breaks on Windows and
the BSDs. If you run the app outside Linux, that is the line to change.

## Testing against a real server

There is no test suite. The app talks to a live Plex DVR, and the interesting
failures are all in what that server actually returns, so the checks that
matter are made against one.

If you test against your own server, remember that creating a recording is
real. Clean up what you make.
