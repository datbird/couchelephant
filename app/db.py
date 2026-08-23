"""SQLite storage. One file, WAL, no ORM.

The guide is a cache: it is re-fetched whole and upserted, so any row can be
rebuilt from Plex at any time. The tables that matter are `passes` and
`pass_actions`, because those are ours and cannot be recovered from Plex.
"""
import json
import os
import sqlite3
import threading
import uuid
from contextlib import contextmanager

DB_PATH = os.environ.get("COUCHELEPHANT_DB", "/data/couchelephant.db")
_local = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS channels (
    vcn        TEXT PRIMARY KEY,
    call_sign  TEXT,
    title      TEXT,
    identifier TEXT,
    thumb_url  TEXT,
    logo_path  TEXT,
    logo_source TEXT,
    logo_fetched_at INTEGER,
    logo_attempts INTEGER DEFAULT 0,
    updated_at INTEGER
);

-- One row per programme. Sports games are episodes of a parent like
-- "NFL Football", so grandparent_title is the league and title is the matchup.
CREATE TABLE IF NOT EXISTS programs (
    guid                  TEXT PRIMARY KEY,
    rating_key            TEXT,
    title                 TEXT,
    grandparent_title     TEXT,
    summary               TEXT,
    type                  TEXT,
    section               TEXT,
    genres                TEXT,
    teams                 TEXT,
    thumb                 TEXT,
    art                   TEXT,
    originally_available  TEXT,
    year                  INTEGER,
    content_rating        TEXT,
    duration              INTEGER,
    updated_at            INTEGER
);

-- One row per BROADCAST. The same game appears here several times, once per
-- channel and time. This distinction is the whole point of the project.
CREATE TABLE IF NOT EXISTS airings (
    id                 TEXT PRIMARY KEY,
    program_guid       TEXT NOT NULL,
    channel_vcn        TEXT,
    channel_call_sign  TEXT,
    channel_identifier TEXT,
    channel_title      TEXT,
    begins_at          INTEGER,
    ends_at            INTEGER,
    premiere           INTEGER DEFAULT 0,
    resolution         TEXT,
    drm                INTEGER DEFAULT 0,
    updated_at         INTEGER,
    FOREIGN KEY (program_guid) REFERENCES programs(guid)
);
CREATE INDEX IF NOT EXISTS idx_airings_begins ON airings(begins_at);
CREATE INDEX IF NOT EXISTS idx_airings_program ON airings(program_guid);
CREATE INDEX IF NOT EXISTS idx_airings_channel ON airings(channel_vcn, begins_at);

CREATE TABLE IF NOT EXISTS teams (
    id         INTEGER PRIMARY KEY,
    name       TEXT,
    -- Which league it plays in, and when it was last seen in the guide.
    -- Teams are kept once seen rather than deleted when they stop playing,
    -- so the list you pick from grows over a season instead of shrinking to
    -- whoever is on this week.
    league     TEXT,
    in_guide   INTEGER DEFAULT 0,
    last_seen  INTEGER,
    updated_at INTEGER
);

-- Mirror of what Plex itself has scheduled, so the UI can show recurring
-- ("All new episodes of X") alongside one-off recordings.
CREATE TABLE IF NOT EXISTS plex_subscriptions (
    key             TEXT PRIMARY KEY,
    title           TEXT,
    type            TEXT,
    target_section  TEXT,
    settings        TEXT,
    created_at      INTEGER,
    updated_at      INTEGER,
    owned_by_us     INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS plex_grabs (
    id            TEXT PRIMARY KEY,
    subscription  TEXT,
    status        TEXT,
    title         TEXT,
    parent_title  TEXT,
    channel_vcn   TEXT,
    begins_at     INTEGER,
    ends_at       INTEGER,
    updated_at    INTEGER
);

-- Ours. A pass says "follow this team"; the scheduler turns it into pinned
-- one-shot recordings on the airing we choose.
CREATE TABLE IF NOT EXISTS passes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    kind         TEXT NOT NULL DEFAULT 'team',
    team_id      INTEGER,
    team_name    TEXT,
    -- A series rule follows one programme rather than a team.
    series_guid  TEXT,
    series_title TEXT,
    -- Two allowlists, JSON lists. Empty or null on both means anywhere. Set
    -- either and an airing has to match one of them, which is how "only ABC,
    -- CBS and FOX" is said. Plex has no equivalent: its rules take one channel.
    networks     TEXT,
    channels     TEXT,
    -- A smart pass carries a condition tree instead of a team or a series.
    filter       TEXT,
    label        TEXT,
    -- Stable across machines, unlike `id`. This is what an export, a backing
    -- store or a restore uses to say "the same pass".
    uid          TEXT,
    enabled      INTEGER DEFAULT 1,
    created_at   INTEGER
);

-- Audit trail. Every decision is written here, including the ones we skipped
-- and why, so the UI can explain itself instead of being a black box.
CREATE TABLE IF NOT EXISTS pass_actions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    pass_id           INTEGER,
    program_guid      TEXT,
    airing_id         TEXT,
    program_title     TEXT,
    channel_vcn       TEXT,
    begins_at         INTEGER,
    action            TEXT,
    reason            TEXT,
    plex_subscription TEXT,
    dry_run           INTEGER DEFAULT 0,
    created_at        INTEGER
);
CREATE INDEX IF NOT EXISTS idx_actions_pass ON pass_actions(pass_id, created_at DESC);

-- Recordings CouchElephant created. Plex has no field saying who asked, so
-- the distinction has to be kept here.
CREATE TABLE IF NOT EXISTS our_grabs (
    airing_id    TEXT PRIMARY KEY,
    program_guid TEXT,
    title        TEXT,
    channel_vcn  TEXT,
    begins_at    INTEGER,
    source       TEXT,
    subscription TEXT,
    pass_uid     TEXT,
    created_at   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ourgrabs_guid ON our_grabs(program_guid);

CREATE TABLE IF NOT EXISTS sync_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at INTEGER,
    ended_at   INTEGER,
    ok         INTEGER,
    detail     TEXT
);
"""

DEFAULTS = {
    "plex_url": "",
    "plex_token": "",
    "timezone": "UTC",
    "sync_minutes": "60",
    # Start in preview. Nothing is written to Plex until this is turned off,
    # so the first run can be inspected before it is trusted.
    "dry_run": "1",
    "epg_provider": "",
    "sports_section": "",
    "shows_section": "",
    "movies_section": "",
}


def connect():
    if not hasattr(_local, "conn"):
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
    return _local.conn


@contextmanager
def tx():
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


# Columns added after the first release. CREATE TABLE IF NOT EXISTS will not
# add them to a database that already exists, so they are applied by hand.
MIGRATIONS = [
    ("channels", "thumb_url", "TEXT"),
    ("channels", "logo_path", "TEXT"),
    # What we actually fetched, so a changed upstream URL triggers a re-fetch.
    ("channels", "logo_source", "TEXT"),
    ("channels", "logo_fetched_at", "INTEGER"),
    ("channels", "logo_attempts", "INTEGER DEFAULT 0"),
    # The Plex subscription a recording of ours created, so it can be cancelled
    # again from the same place it was scheduled.
    ("our_grabs", "subscription", "TEXT"),
    # The network a channel carries, pulled out of the guide's channel title.
    ("channels", "network", "TEXT"),
    # A rule can be limited to a set of networks, which Plex cannot express:
    # its own rules take one channel or none.
    ("passes", "networks", "TEXT"),
    ("passes", "channels", "TEXT"),
    # A logo the user supplied, which wins over whatever the guide offers. Kept
    # in its own column so a re-fetch of the guide's own art cannot touch it.
    ("channels", "custom_logo", "TEXT"),
    ("channels", "custom_logo_at", "INTEGER"),
    # Which rule booked this, so the schedule can say why a thing records.
    ("our_grabs", "pass_id", "INTEGER"),
    # Plex's own recording settings, kept on the pass and applied to every
    # broadcast it books, so padding and quality are not lost by using a pass.
    ("passes", "prefs", "TEXT"),
    # What a Plex rule follows. Its own title is the generic template name,
    # "All Episodes", which says nothing about which programme.
    ("plex_subscriptions", "target", "TEXT"),
    ("passes", "series_guid", "TEXT"),
    ("passes", "series_title", "TEXT"),
    # The parental rating and the running time, so a smart filter can ask about
    # them. Both come from the guide listing and are empty until the next sync.
    ("programs", "content_rating", "TEXT"),
    ("programs", "duration", "INTEGER"),
    # A smart pass keeps its condition tree here, as JSON. Plex cannot express
    # one, so this kind of pass is always CouchElephant's to run.
    ("passes", "filter", "TEXT"),
    ("passes", "label", "TEXT"),
    # Teams are now remembered rather than dropped when they stop playing.
    ("teams", "league", "TEXT"),
    ("teams", "in_guide", "INTEGER DEFAULT 0"),
    ("teams", "last_seen", "INTEGER"),
    # A pass's `id` is an autoincrement, which means a different number on
    # every install. Anything that leaves this machine, an export, a backing
    # store, a snapshot restored elsewhere, needs a name for a pass that two
    # machines agree on. See [[dbstore]].
    ("passes", "uid", "TEXT"),
    ("our_grabs", "pass_uid", "TEXT"),
]


def _migrate(conn):
    for table, column, decl in MIGRATIONS:
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init():
    conn = connect()
    conn.executescript(SCHEMA)
    _migrate(conn)
    for k, v in DEFAULTS.items():
        conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)", (k, v))
    _backfill_uids(conn)
    # After the migration and the backfill, not inside SCHEMA: on an existing
    # install the column does not exist until _migrate has run.
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS passes_uid ON passes(uid)")
    conn.commit()


def _backfill_uids(conn):
    """Give every existing pass a uid, and every grab the uid of its pass.

    A pass made before this column existed has none, so anything that names a
    pass across machines would silently skip it.
    """
    rows = conn.execute("SELECT id FROM passes WHERE uid IS NULL OR uid = ''").fetchall()
    for r in rows:
        conn.execute("UPDATE passes SET uid = ? WHERE id = ?",
                     (uuid.uuid4().hex, r["id"]))
    conn.execute(
        "UPDATE our_grabs SET pass_uid = (SELECT uid FROM passes WHERE passes.id = "
        "our_grabs.pass_id) WHERE pass_uid IS NULL AND pass_id IS NOT NULL")


def get_setting(key, default=None):
    row = connect().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else (default if default is not None else DEFAULTS.get(key))


def all_settings():
    rows = connect().execute("SELECT key, value FROM settings").fetchall()
    out = dict(DEFAULTS)
    out.update({r["key"]: r["value"] for r in rows})
    return out


def set_setting(key, value):
    with tx() as c:
        c.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, str(value)),
        )


def query(sql, params=()):
    return connect().execute(sql, params).fetchall()


def one(sql, params=()):
    return connect().execute(sql, params).fetchone()


def js(value):
    return json.dumps(value, separators=(",", ":"))


def unjs(value, default=None):
    try:
        return json.loads(value) if value else (default if default is not None else [])
    except Exception:
        return default if default is not None else []
