"""Durable data: what it is, how it leaves this machine, and how it comes back.

Most of what CouchElephant holds is a cache. The guide, the channels, the teams
and Plex's own schedule are all pulled from Plex and rebuilt in seconds. Copying
20,000 airings to an external database would be slow, large and pointless: they
are an output, not data.

What cannot be rebuilt is what you decided. Your passes, their conditions and
their source limits. The recordings you booked. The channel artwork you
supplied. Your settings and your accounts. That is what this module carries.

Three things are built on it, and they protect against different accidents:

    Export / import       one file, moved by hand
    Snapshot backups      a copy of a moment, in app/backups.py
    Backing store         a live two-way replica in an external database

A live replica saves you from losing the machine. A snapshot saves you from a
change you regret, because a replica faithfully copies the mistake.

FOUR RULES, each of them a bug somewhere else first:

1. A failed remote read must RAISE, never return a short set. A three-way merge
   reading an empty remote against a populated shadow concludes the remote
   deleted everything, and deletes it locally. A transient 500 is enough.
2. A store's key must be stable across machines. `passes.id` is an
   autoincrement and means a different pass on another install, so passes carry
   a `uid` and grabs carry `pass_uid`.
3. Nothing here writes the guide. A restore must not resurrect a stale
   programme; the next sync brings the real one.
4. A secret leaves only when asked. The Plex token is a credential, and an
   export is a file that gets emailed.
"""
import hashlib
import json
import os

from . import auth, db

# ---------------------------------------------------------------- the stores
#
# `key` is the tuple of columns that names a row the same way on ANY machine.
# `pk`  is this table's real primary key, which is what an upsert conflicts on.
#       They differ only where a local key is an autoincrement the wire cannot
#       use: prefs are keyed by username outside and by user_id inside.
# `cols` is what travels; None means every column the table has.
# `where` narrows a table to the part that is actually the user's.

STORES = {
    "settings": {
        "table": "settings", "key": ("key",), "pk": ("key",), "cols": None, "db": "main",
        "label": "Settings",
    },
    "passes": {
        # `uid`, not `id`. See rule 2.
        "table": "passes", "key": ("uid",), "pk": ("uid",), "db": "main",
        "cols": ("uid", "kind", "team_id", "team_name", "series_guid",
                 "series_title", "networks", "channels", "prefs", "filter",
                 "label", "enabled", "created_at"),
        "label": "Passes",
    },
    "our_grabs": {
        "table": "our_grabs", "key": ("airing_id",), "pk": ("airing_id",), "db": "main",
        "cols": ("airing_id", "program_guid", "title", "channel_vcn",
                 "begins_at", "source", "subscription", "pass_uid", "created_at"),
        "label": "Recordings we booked",
    },
    "channel_art": {
        # Only the artwork you supplied. The rest of the row is guide data.
        "table": "channels", "key": ("vcn",), "pk": ("vcn",), "db": "main",
        "cols": ("vcn", "custom_logo", "custom_logo_at"),
        "where": "custom_logo IS NOT NULL AND custom_logo != ''",
        "label": "Channel artwork",
    },
    "users": {
        "table": "users", "key": ("username",), "pk": ("username",), "db": "auth",
        "cols": ("username", "pw_hash", "pw_salt", "role", "created_at"),
        "label": "Accounts",
    },
    "user_prefs": {
        # Keyed by username rather than by the local user_id, which differs
        # between installs. `_read`/`_apply` translate.
        "table": "prefs", "key": ("username", "key"), "pk": ("user_id", "key"), "db": "auth",
        "cols": ("username", "key", "value"),
        "label": "Account preferences",
    },
    "email_map": {
        "table": "email_map", "key": ("email",), "pk": ("email",), "db": "auth",
        "cols": ("email", "username", "created_at"),
        "label": "Cloudflare identities",
    },
}

# Kept out of an export unless the user asks. A token in a file is a token in
# whatever the file lands in.
SECRET_SETTINGS = frozenset(("plex_token", "cf_aud", "pg_password", "my_password"))

# Never durable, whoever asks. These describe what this machine last did, not
# anything anybody decided. `backingstore_status` in particular is written by
# the sync itself, so carrying it made every run find one changed record and
# push it, for ever.
TRANSIENT_SETTINGS = frozenset(("backingstore_status",
                                # The last health reading of Plex's own guide.
                                # A snapshot of another machine's Plex, not a
                                # decision of this user's worth carrying.
                                "epg_refreshed_at", "guide_ends_at"))

# What a restore must never bring back. These rebuild from Plex on the next
# sync, and a stale copy is worse than an empty one.
CACHE_TABLES = ("programs", "airings", "channels", "teams", "plex_subscriptions",
                "plex_grabs", "sync_log", "notices")


def _con(which):
    return auth._con() if which == "auth" else db.connect()


def _usernames():
    """id -> username, for the auth tables keyed on a local id."""
    return {r["id"]: r["username"] for r in
            _con("auth").execute("SELECT id, username FROM users")}


def _ids():
    return {r["username"]: r["id"] for r in
            _con("auth").execute("SELECT id, username FROM users")}


# ---------------------------------------------------------------- reading

def read(name: str, include_secrets: bool = False) -> dict[str, dict]:
    """One store as {key_tuple: row_dict}. Keys are strings, always."""
    spec = STORES[name]
    con = _con(spec["db"])
    have = {r[1] for r in con.execute(f"PRAGMA table_info({spec['table']})")}
    cols = [c for c in (spec["cols"] or sorted(have)) if c in have]

    if name in ("user_prefs", "email_map"):
        # These carry user_id locally and username on the wire.
        cols = [c for c in cols if c != "username"] + ["user_id"]

    sql = f"SELECT {', '.join(cols)} FROM {spec['table']}"
    if spec.get("where"):
        sql += " WHERE " + spec["where"]
    out = {}
    names = _usernames() if name in ("user_prefs", "email_map") else {}
    for row in con.execute(sql):
        rec = {c: row[c] for c in cols}
        if name in ("user_prefs", "email_map"):
            who = names.get(rec.pop("user_id"))
            if not who:
                continue        # an orphan preference belongs to nobody
            rec["username"] = who
        if name == "settings":
            if rec["key"] in TRANSIENT_SETTINGS:
                continue
            if not include_secrets and rec["key"] in SECRET_SETTINGS:
                continue
        if name == "channel_art":
            # The row holds an absolute path. Only its name travels: the other
            # install keeps its logos wherever it keeps them.
            rec["custom_logo"] = os.path.basename(rec["custom_logo"])
        out[_key(spec, rec)] = rec
    return out


def _logo_dir():
    return os.environ.get("COUCHELEPHANT_LOGOS", "/data/logos")


def _key(spec, rec):
    return "\x1f".join("" if rec.get(c) is None else str(rec.get(c))
                       for c in spec["key"])


def snapshot(include_secrets: bool = False) -> dict[str, dict[str, dict]]:
    """Every durable store, ready to be written somewhere."""
    return {name: read(name, include_secrets) for name in STORES}


def fingerprint(rec: dict) -> str:
    """A stable hash of one record, for telling a change from a re-read."""
    canon = json.dumps({k: ("" if v is None else str(v)) for k, v in sorted(rec.items())},
                       sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()[:32]


# ---------------------------------------------------------------- writing

def apply(name: str, rows: list[dict], delete_missing: bool = False) -> tuple[int, int]:
    """Write records into a store. Returns (written, deleted).

    `delete_missing` removes local rows the incoming set does not carry, which
    is what a restore wants and what a merge must be told explicitly.
    """
    spec = STORES[name]
    con = _con(spec["db"])
    have = {r[1] for r in con.execute(f"PRAGMA table_info({spec['table']})")}
    ids = _ids() if name in ("user_prefs", "email_map") else {}

    written = 0
    for rec in rows:
        rec = dict(rec)
        if name == "settings" and rec.get("key") in TRANSIENT_SETTINGS:
            continue
        if name in ("user_prefs", "email_map"):
            uid = ids.get(rec.pop("username", None))
            if uid is None:
                continue        # its account did not come with it
            rec["user_id"] = uid
        if name == "channel_art" and rec.get("custom_logo"):
            rec["custom_logo"] = os.path.join(_logo_dir(),
                                              os.path.basename(rec["custom_logo"]))
        rec = {k: v for k, v in rec.items() if k in have}
        if not rec:
            continue
        cols = sorted(rec)
        # Conflict on the table's OWN primary key, not on the portable one.
        keys = [c for c in spec["pk"] if c in cols] or cols[:1]
        sets = [c for c in cols if c not in keys]
        sql = (f"INSERT INTO {spec['table']} ({', '.join(cols)}) "
               f"VALUES ({', '.join('?' * len(cols))})")
        if sets:
            sql += (f" ON CONFLICT({', '.join(keys)}) DO UPDATE SET "
                    + ", ".join(f"{c}=excluded.{c}" for c in sets))
        else:
            sql += f" ON CONFLICT({', '.join(keys)}) DO NOTHING"
        con.execute(sql, [rec[c] for c in cols])
        written += 1

    deleted = 0
    if delete_missing:
        keep = {_key(spec, dict(r)) for r in rows}
        for k in list(read(name, include_secrets=True)):
            if k not in keep:
                parts = k.split("\x1f")
                where = " AND ".join(f"{c} = ?" for c in spec["key"])
                con.execute(f"DELETE FROM {spec['table']} WHERE {where}", parts)
                deleted += 1
    con.commit()
    return written, deleted


def relink_passes() -> None:
    """Point every grab at the local id of the pass its uid names.

    An import brings `pass_uid`, which is portable, and knows nothing about
    this machine's `passes.id`. Without this the schedule cannot say which pass
    booked a recording.
    """
    with db.tx() as c:
        c.execute("UPDATE our_grabs SET pass_id = "
                  "(SELECT id FROM passes WHERE passes.uid = our_grabs.pass_uid) "
                  "WHERE pass_uid IS NOT NULL AND pass_uid != ''")


# ---------------------------------------------------------------- the merge

def merge(local: dict, remote: dict, shadow: dict) -> tuple[dict, dict, list, list, list]:
    """Three-way merge of one store. Pure, so it can be reasoned about.

    `shadow` is what both sides looked like at the last sync, as
    {key: fingerprint}. Comparing against it is what separates a real local
    change from a real remote one, so an edit made anywhere reaches everywhere
    and a delete is a delete rather than "the other side is missing a row".

    Returns (to_local, to_remote, to_delete_local, to_delete_remote, conflicts).
    """
    to_local, to_remote = {}, {}
    del_local, del_remote, conflicts = [], [], []

    for key in set(local) | set(remote) | set(shadow):
        mine, theirs = local.get(key), remote.get(key)
        was = shadow.get(key)
        lf = fingerprint(mine) if mine is not None else None
        rf = fingerprint(theirs) if theirs is not None else None

        if lf == rf:
            continue                                   # already agree
        l_changed = lf != was
        r_changed = rf != was

        if l_changed and not r_changed:
            if mine is not None:
                to_remote[key] = mine
            else:
                del_remote.append(key)
        elif r_changed and not l_changed:
            if theirs is not None:
                to_local[key] = theirs
            else:
                del_local.append(key)
        else:
            # Both moved since the last sync.
            conflicts.append(key)
            if mine is None and theirs is not None:
                # An edit beats a delete. Losing an edit loses work; losing a
                # delete costs one more click.
                to_local[key] = theirs
            elif theirs is None and mine is not None:
                to_remote[key] = mine
            elif _newer(mine, theirs):
                to_remote[key] = mine
            else:
                to_local[key] = theirs
    return to_local, to_remote, del_local, del_remote, conflicts


def _newer(mine, theirs):
    """Whichever record claims the later timestamp. Local wins a tie."""
    def stamp(rec):
        for col in ("updated_at", "created_at", "custom_logo_at", "last_seen"):
            v = rec.get(col)
            if v not in (None, ""):
                try:
                    return int(v)
                except (TypeError, ValueError):
                    pass
        return 0
    return stamp(mine) >= stamp(theirs)


def new_shadow(local: dict, remote: dict) -> dict[str, str]:
    """What both sides look like now, for the next merge to compare against."""
    out = {}
    for key in set(local) | set(remote):
        rec = local.get(key, remote.get(key))
        out[key] = fingerprint(rec)
    return out


# ---------------------------------------------------------------- the shadow

SHADOW_SCHEMA = """
CREATE TABLE IF NOT EXISTS sync_shadow (
    backend TEXT,
    store   TEXT,
    k       TEXT,
    fp      TEXT,
    PRIMARY KEY (backend, store, k)
);
"""


def load_shadow(backend: str, store: str) -> dict[str, str]:
    con = db.connect()
    con.executescript(SHADOW_SCHEMA)
    return {r["k"]: r["fp"] for r in con.execute(
        "SELECT k, fp FROM sync_shadow WHERE backend = ? AND store = ?",
        (backend, store))}


def save_shadow(backend: str, store: str, shadow: dict[str, str]) -> None:
    con = db.connect()
    con.executescript(SHADOW_SCHEMA)
    con.execute("DELETE FROM sync_shadow WHERE backend = ? AND store = ?",
                (backend, store))
    con.executemany(
        "INSERT INTO sync_shadow (backend, store, k, fp) VALUES (?,?,?,?)",
        [(backend, store, k, fp) for k, fp in shadow.items()])
    con.commit()


def forget_shadow(backend: str) -> None:
    con = db.connect()
    con.executescript(SHADOW_SCHEMA)
    con.execute("DELETE FROM sync_shadow WHERE backend = ?", (backend,))
    con.commit()
