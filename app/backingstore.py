"""The backing store: a live two-way replica of your decisions.

Point CouchElephant at another database and it keeps that database and this one
in step, both ways. Change a pass here, it appears there. Restore this machine
from nothing, or set up a second one, and it pulls everything back.

The local SQLite is always the working store. The external database is a
durable replica, reconciled on demand or on a timer. There is deliberately no
"remote primary" mode: it would be a network hop per query and buys nothing.

Every backend stores the same shape, one table per store, two columns:

    couchelephant_<store>(k TEXT PRIMARY KEY, data TEXT)

`data` is the record as JSON. Per-column tables were tried elsewhere and
collided with system column names; a blob dodges that and keeps types.

THE RULE THAT MATTERS MOST: a failed read raises. It never returns a short set.
A three-way merge that reads an empty remote against a populated shadow
concludes the remote deleted everything, and deletes it here. One transient 500
is enough to lose the lot.
"""
import json
import os
import sqlite3
import time

from . import db, dbstore

PREFIX = "couchelephant_"


class BackendError(RuntimeError):
    """Something the user can act on: unreachable, refused, misconfigured."""


# ---------------------------------------------------------------- backends

class Backend:
    name = ""
    label = ""
    fields = ()          # (key, label, kind) shown in Settings

    def __init__(self, cfg):
        self.cfg = cfg

    def test(self):
        """Connect and touch a store. Returns a sentence for the user."""
        raise NotImplementedError

    def read_all(self, store):
        """{k: record}. MUST raise rather than return a short set."""
        raise NotImplementedError

    def write(self, store, records, deletes):
        raise NotImplementedError


class SqliteBackend(Backend):
    """A SQLite file, somewhere else. A NAS share, a synced folder, a stick.

    The least there is to go wrong, and it needs nothing installed.
    """
    name = "sqlite"
    label = "SQLite file"
    fields = (("sqlite_path", "File path", "text"),)

    def _path(self):
        path = (self.cfg.get("sqlite_path") or "").strip()
        if not path:
            raise BackendError("Give the path of the file to keep the copy in.")
        return path

    def _con(self):
        path = self._path()
        parent = os.path.dirname(path) or "."
        if not os.path.isdir(parent):
            raise BackendError(f"There is no folder {parent} that this container "
                               "can see. Mount it, or pick another path.")
        try:
            con = sqlite3.connect(path, timeout=30)
        except sqlite3.Error as e:
            raise BackendError(f"Could not open {path}: {e}")
        con.row_factory = sqlite3.Row
        return con

    def test(self):
        # read_all creates the store tables as it goes, so a good test leaves
        # the file exactly as a sync would, with nothing extra in it.
        n = sum(len(self.read_all(s)) for s in dbstore.STORES)
        return f"Wrote to {self._path()}. It holds {n} record(s)."

    def read_all(self, store):
        con = self._con()
        table = PREFIX + store
        try:
            with con:
                con.execute(f"CREATE TABLE IF NOT EXISTS {table} "
                            "(k TEXT PRIMARY KEY, data TEXT)")
                rows = con.execute(f"SELECT k, data FROM {table}").fetchall()
        except sqlite3.Error as e:
            # Raise. Never hand back a short set. See the module docstring.
            raise BackendError(f"Could not read {table}: {e}")
        return {r["k"]: json.loads(r["data"]) for r in rows}

    def write(self, store, records, deletes):
        con = self._con()
        table = PREFIX + store
        try:
            with con:
                con.execute(f"CREATE TABLE IF NOT EXISTS {table} "
                            "(k TEXT PRIMARY KEY, data TEXT)")
                for k, rec in records.items():
                    con.execute(
                        f"INSERT INTO {table} (k, data) VALUES (?,?) "
                        "ON CONFLICT(k) DO UPDATE SET data=excluded.data",
                        (k, json.dumps(rec, sort_keys=True)))
                for k in deletes:
                    con.execute(f"DELETE FROM {table} WHERE k = ?", (k,))
        except sqlite3.Error as e:
            raise BackendError(f"Could not write {table}: {e}")


class _SqlBackend(Backend):
    """Shared shape for the server databases. Only the driver differs."""
    placeholder = "%s"
    upsert = ""

    def _connect(self):
        raise NotImplementedError

    def _ddl(self, table):
        return (f"CREATE TABLE IF NOT EXISTS {table} "
                "(k VARCHAR(255) PRIMARY KEY, data TEXT)")

    # `with connection:` means "one transaction" to psycopg and "close me
    # afterwards" to PyMySQL. Committing and closing by hand is the only thing
    # both drivers read the same way.
    def _run(self, what, fn):
        con = self._connect()
        try:
            cur = con.cursor()
            out = fn(cur)
            con.commit()
            return out
        except BackendError:
            raise
        except Exception as e:
            try:
                con.rollback()
            except Exception:
                pass
            raise BackendError(f"Could not {what}: {e}")
        finally:
            try:
                con.close()
            except Exception:
                pass

    def test(self):
        n = sum(len(self.read_all(s)) for s in dbstore.STORES)
        return f"Connected. It holds {n} record(s)."

    def read_all(self, store):
        table = PREFIX + store

        def go(cur):
            cur.execute(self._ddl(table))
            cur.execute(f"SELECT k, data FROM {table}")
            return cur.fetchall()

        rows = self._run(f"read {table}", go)
        return {r[0]: json.loads(r[1]) for r in rows}

    def write(self, store, records, deletes):
        table = PREFIX + store
        p = self.placeholder

        def go(cur):
            cur.execute(self._ddl(table))
            for k, rec in records.items():
                cur.execute(
                    f"INSERT INTO {table} (k, data) VALUES ({p},{p}) " + self.upsert,
                    (k, json.dumps(rec, sort_keys=True)))
            for k in deletes:
                cur.execute(f"DELETE FROM {table} WHERE k = {p}", (k,))

        self._run(f"write {table}", go)


class PostgresBackend(_SqlBackend):
    name = "postgres"
    label = "PostgreSQL"
    upsert = "ON CONFLICT (k) DO UPDATE SET data = EXCLUDED.data"
    fields = (("pg_host", "Host", "text"), ("pg_port", "Port", "text"),
              ("pg_db", "Database", "text"), ("pg_user", "User", "text"),
              ("pg_password", "Password", "secret"))

    def _connect(self):
        try:
            import psycopg
        except ImportError:
            raise BackendError("PostgreSQL support needs the psycopg driver, which "
                               "is not in this image.")
        try:
            return psycopg.connect(
                host=self.cfg.get("pg_host") or "127.0.0.1",
                port=int(self.cfg.get("pg_port") or 5432),
                dbname=self.cfg.get("pg_db") or "couchelephant",
                user=self.cfg.get("pg_user") or "",
                password=self.cfg.get("pg_password") or "",
                connect_timeout=15)
        except Exception as e:
            raise BackendError(f"Could not reach PostgreSQL: {e}")


class MysqlBackend(_SqlBackend):
    name = "mysql"
    label = "MySQL or MariaDB"
    upsert = "ON DUPLICATE KEY UPDATE data = VALUES(data)"
    fields = (("my_host", "Host", "text"), ("my_port", "Port", "text"),
              ("my_db", "Database", "text"), ("my_user", "User", "text"),
              ("my_password", "Password", "secret"))

    def _connect(self):
        try:
            import pymysql
        except ImportError:
            raise BackendError("MySQL support needs the PyMySQL driver, which is "
                               "not in this image.")
        try:
            return pymysql.connect(
                host=self.cfg.get("my_host") or "127.0.0.1",
                port=int(self.cfg.get("my_port") or 3306),
                database=self.cfg.get("my_db") or "couchelephant",
                user=self.cfg.get("my_user") or "",
                password=self.cfg.get("my_password") or "",
                connect_timeout=15)
        except Exception as e:
            raise BackendError(f"Could not reach MySQL: {e}")


BACKENDS = {b.name: b for b in (SqliteBackend, PostgresBackend, MysqlBackend)}

CONFIG_KEYS = ["backingstore_backend", "backingstore_auto_minutes"]
for _b in BACKENDS.values():
    CONFIG_KEYS += [f[0] for f in _b.fields]

SECRET_KEYS = frozenset(
    f[0] for b in BACKENDS.values() for f in b.fields if f[2] == "secret")


def config():
    return {k: db.get_setting(k) or "" for k in CONFIG_KEYS}


def chosen():
    """The configured backend, ready to use, or None."""
    name = (db.get_setting("backingstore_backend") or "").strip()
    if name not in BACKENDS:
        return None
    return BACKENDS[name](config())


# ---------------------------------------------------------------- the run

def _status(**kw):
    db.set_setting("backingstore_status", json.dumps(kw))
    return kw


def status():
    try:
        return json.loads(db.get_setting("backingstore_status") or "{}")
    except ValueError:
        return {}


def sync_all(dry_run=False):
    """Reconcile every durable store, both ways. Returns a per-store report."""
    backend = chosen()
    if backend is None:
        raise BackendError("No backing store is configured.")

    report, pushed, pulled, gone = {}, 0, 0, 0
    for name in dbstore.STORES:
        local = dbstore.read(name, include_secrets=True)
        remote = backend.read_all(name)          # raises rather than short-reads
        shadow = dbstore.load_shadow(backend.name, name)

        to_local, to_remote, del_local, del_remote, conflicts = dbstore.merge(
            local, remote, shadow)

        report[name] = {
            "pull": len(to_local), "push": len(to_remote),
            "delete_local": len(del_local), "delete_remote": len(del_remote),
            "conflicts": len(conflicts),
        }
        pushed += len(to_remote)
        pulled += len(to_local)
        gone += len(del_local) + len(del_remote)
        if dry_run:
            continue

        if to_remote or del_remote:
            backend.write(name, to_remote, del_remote)
        if to_local:
            dbstore.apply(name, list(to_local.values()))
        for key in del_local:
            _delete_local(name, key)

        # What both sides look like now, so the next run can tell a change
        # from a re-read. The remote is what it held plus what was just
        # written, so there is no need to ask it again.
        remote = {k: v for k, v in remote.items() if k not in del_remote}
        remote.update(to_remote)
        local = dbstore.read(name, include_secrets=True)
        dbstore.save_shadow(backend.name, name, dbstore.new_shadow(local, remote))

    if not dry_run:
        dbstore.relink_passes()
        _status(ok=True, at=int(time.time()), backend=backend.name,
                pushed=pushed, pulled=pulled, removed=gone,
                detail=f"{pushed} sent, {pulled} received, {gone} removed")
    return {"ok": True, "dry_run": dry_run, "backend": backend.name,
            "pushed": pushed, "pulled": pulled, "removed": gone, "stores": report}


def _delete_local(name, key):
    spec = dbstore.STORES[name]
    con = dbstore._con(spec["db"])
    parts = key.split("\x1f")
    if name in ("user_prefs", "email_map"):
        # The portable key names a username; the row is keyed on user_id.
        ids = dbstore._ids()
        who = ids.get(parts[0])
        if who is None:
            return
        parts = [who] + parts[1:]
    where = " AND ".join(f"{c} = ?" for c in spec["pk"])
    con.execute(f"DELETE FROM {spec['table']} WHERE {where}", parts[:len(spec['pk'])])
    con.commit()


def restore_from_remote(dry_run=False):
    """Pull everything down, and write nothing back.

    A plain sync would be wrong here. Restoring onto an empty machine, the
    merge reads every missing row as a local delete and erases the very copy
    you are restoring from. So this only writes locally, then rewrites the
    shadow to match what it pulled, which makes the next ordinary sync a
    clean no-op.
    """
    backend = chosen()
    if backend is None:
        raise BackendError("No backing store is configured.")

    report, total = {}, 0
    for name in dbstore.STORES:
        remote = backend.read_all(name)
        report[name] = len(remote)
        total += len(remote)
        if dry_run:
            continue
        dbstore.apply(name, list(remote.values()))
        dbstore.save_shadow(backend.name, name,
                            {k: dbstore.fingerprint(v) for k, v in remote.items()})
    if not dry_run:
        dbstore.relink_passes()
        db.init()
        _status(ok=True, at=int(time.time()), backend=backend.name,
                pushed=0, pulled=total, removed=0,
                detail=f"restored {total} record(s) from {backend.name}")
    return {"ok": True, "dry_run": dry_run, "restored": total, "stores": report}
