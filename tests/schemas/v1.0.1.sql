-- The database schema exactly as CouchElephant 1.0.1 shipped it.
-- Frozen on purpose. `tests/test_upgrade.py` upgrades a database
-- built from this and checks it ends up the same shape as a fresh
-- install. Do not regenerate it: that is the check.
-- Taken from `git show v1.0.1:app/db.py`.

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
