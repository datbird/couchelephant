"""Snapshot backups: a copy of a moment, kept on a schedule.

A backing store saves you from losing the machine. A snapshot saves you from a
change you regret, because a replica faithfully copies the mistake. They are
different jobs and both are here.

A job says what to keep, where to put it, how often, and how many to keep. Each
run writes one zip, which is an export plus, if asked, the raw database files.

Encryption is optional and off by default. When a passphrase is set the zip is
AES-256, which 7-Zip, Keka and WinRAR can all open. A backup only this program
can read is not much of a backup.

The database files are copied with SQLite's own online backup, so a snapshot
taken mid-write is consistent rather than a torn page.
"""
import io
import json
import os
import re
import shutil
import sqlite3
import time
import zipfile

from . import db, portable

SCHEMA = """
CREATE TABLE IF NOT EXISTS backup_jobs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT,
    enabled      INTEGER DEFAULT 1,
    dest_path    TEXT,
    every_hours  INTEGER DEFAULT 24,
    retention    INTEGER DEFAULT 7,
    passphrase   TEXT,
    raw_db       INTEGER DEFAULT 1,
    with_secrets INTEGER DEFAULT 0,
    last_run     INTEGER,
    last_ok      INTEGER,
    last_error   TEXT,
    last_file    TEXT,
    last_size    INTEGER,
    created_at   INTEGER
);
"""


def init():
    con = db.connect()
    con.executescript(SCHEMA)
    con.commit()


def _slug(name):
    s = re.sub(r"[^a-z0-9]+", "-", (name or "backup").lower()).strip("-")
    return s or "backup"


def jobs():
    init()
    return [_public(r) for r in db.query("SELECT * FROM backup_jobs ORDER BY id")]


def _public(row):
    d = dict(row)
    # Never handed back. It is stored because an unattended run needs it.
    d["encrypted"] = bool(d.pop("passphrase", None))
    d["enabled"] = bool(d["enabled"])
    d["raw_db"] = bool(d["raw_db"])
    d["with_secrets"] = bool(d["with_secrets"])
    return d


def save_job(job_id=None, **f):
    init()
    fields = {
        "name": (f.get("name") or "Backup").strip(),
        "enabled": 1 if f.get("enabled") else 0,
        "dest_path": (f.get("dest_path") or "").strip(),
        "every_hours": max(0, int(f.get("every_hours") or 0)),
        "retention": max(0, int(f.get("retention") or 0)),
        "raw_db": 1 if f.get("raw_db") else 0,
        "with_secrets": 1 if f.get("with_secrets") else 0,
    }
    passphrase = f.get("passphrase")
    with db.tx() as c:
        if job_id:
            sets = ", ".join(f"{k}=?" for k in fields)
            c.execute(f"UPDATE backup_jobs SET {sets} WHERE id=?",
                      list(fields.values()) + [job_id])
            # A blank passphrase field means "leave it alone", not "remove it".
            # Removing it is done by sending the word off.
            if passphrase == "off":
                c.execute("UPDATE backup_jobs SET passphrase=NULL WHERE id=?", (job_id,))
            elif passphrase:
                c.execute("UPDATE backup_jobs SET passphrase=? WHERE id=?",
                          (passphrase, job_id))
            return job_id
        cols = list(fields) + ["passphrase", "created_at"]
        vals = list(fields.values()) + [passphrase or None, int(time.time())]
        c.execute(f"INSERT INTO backup_jobs ({', '.join(cols)}) "
                  f"VALUES ({', '.join('?' * len(cols))})", vals)
        return c.execute("SELECT last_insert_rowid()").fetchone()[0]


def delete_job(job_id):
    init()
    with db.tx() as c:
        c.execute("DELETE FROM backup_jobs WHERE id = ?", (job_id,))


# ---------------------------------------------------------------- running

def _db_paths():
    main = os.environ.get("COUCHELEPHANT_DB", "/data/couchelephant.db")
    auth_db = os.environ.get("COUCHELEPHANT_AUTH_DB",
                             os.path.join(os.path.dirname(main), "auth.db"))
    return {"couchelephant.db": main, "auth.db": auth_db}


def _consistent_copy(path):
    """The file as SQLite itself would copy it, safe mid-write."""
    src = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    buf = io.BytesIO()
    try:
        tmp = path + f".snap{os.getpid()}"
        dst = sqlite3.connect(tmp)
        try:
            src.backup(dst)
        finally:
            dst.close()
        with open(tmp, "rb") as f:
            buf.write(f.read())
        os.unlink(tmp)
    finally:
        src.close()
    return buf.getvalue()


def build(raw_db=True, with_secrets=False, version="", note=""):
    """One archive, in memory. Returns bytes."""
    blob = portable.export_bytes(include_secrets=with_secrets,
                                 version=version, note=note)
    if not raw_db:
        return blob
    # The export plus the databases themselves, so a restore has both the
    # portable form and the exact file.
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        with zipfile.ZipFile(io.BytesIO(blob)) as src:
            for entry in src.namelist():
                z.writestr(entry, src.read(entry))
        for name, path in _db_paths().items():
            if os.path.isfile(path):
                z.writestr(f"sqlite/{name}", _consistent_copy(path))
    return out.getvalue()


def _write(path, data, passphrase):
    if not passphrase:
        with open(path, "wb") as f:
            f.write(data)
        return
    try:
        import pyzipper
    except ImportError:
        raise RuntimeError("Encryption needs the pyzipper package, which is not "
                           "in this image. Clear the passphrase to keep plain zips.")
    with pyzipper.AESZipFile(path, "w", compression=pyzipper.ZIP_DEFLATED,
                             encryption=pyzipper.WZ_AES) as z:
        z.setpassword(passphrase.encode())
        z.writestr("couchelephant-backup.zip", data)


def run_job(job_id, version=""):
    """Run one job now. Records the outcome on the job either way."""
    init()
    row = db.one("SELECT * FROM backup_jobs WHERE id = ?", (job_id,))
    if not row:
        raise FileNotFoundError("no such backup job")
    now = int(time.time())
    dest = (row["dest_path"] or "").strip()
    try:
        if not dest:
            raise RuntimeError("This job has nowhere to write. Give it a folder.")
        os.makedirs(dest, exist_ok=True)
        data = build(raw_db=bool(row["raw_db"]),
                     with_secrets=bool(row["with_secrets"]),
                     version=version, note=f"job {row['name']}")
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
        fname = f"couchelephant-{_slug(row['name'])}-{stamp}.zip"
        path = os.path.join(dest, fname)
        _write(path, data, row["passphrase"])
        size = os.path.getsize(path)
        pruned = _prune(dest, _slug(row["name"]), row["retention"])
        with db.tx() as c:
            c.execute("UPDATE backup_jobs SET last_run=?, last_ok=1, last_error=NULL, "
                      "last_file=?, last_size=? WHERE id=?", (now, fname, size, job_id))
        return {"ok": True, "file": fname, "size": size, "pruned": pruned}
    except Exception as e:
        msg = f"{type(e).__name__}: {e}"
        with db.tx() as c:
            c.execute("UPDATE backup_jobs SET last_run=?, last_ok=0, last_error=? "
                      "WHERE id=?", (now, msg, job_id))
        return {"ok": False, "error": msg}


def _prune(dest, slug, keep):
    """Remove this job's oldest archives. Only ever this job's.

    Matched on the job's own filename prefix, so two jobs writing to one folder
    cannot delete each other's work.
    """
    if not keep:
        return 0
    prefix = f"couchelephant-{slug}-"
    mine = sorted(f for f in os.listdir(dest)
                  if f.startswith(prefix) and f.endswith(".zip"))
    extra = mine[:-keep] if len(mine) > keep else []
    for f in extra:
        try:
            os.unlink(os.path.join(dest, f))
        except OSError:
            pass
    return len(extra)


def archives(dest):
    """Every CouchElephant archive in a folder, newest first."""
    if not dest or not os.path.isdir(dest):
        return []
    out = []
    for f in os.listdir(dest):
        if not (f.startswith("couchelephant-") and f.endswith(".zip")):
            continue
        p = os.path.join(dest, f)
        try:
            st = os.stat(p)
        except OSError:
            continue
        out.append({"name": f, "size": st.st_size, "at": int(st.st_mtime)})
    out.sort(key=lambda a: a["at"], reverse=True)
    return out


def read_archive(dest, name, passphrase=""):
    """The export inside an archive, unwrapped and decrypted if need be."""
    if "/" in name or "\\" in name or not name.endswith(".zip"):
        raise ValueError("that is not an archive name")
    path = os.path.join(dest, name)
    with open(path, "rb") as f:
        data = f.read()
    if not passphrase:
        return data
    import pyzipper
    with pyzipper.AESZipFile(io.BytesIO(data)) as z:
        z.setpassword(passphrase.encode())
        inner = z.namelist()[0]
        return z.read(inner)


def restore(dest, name, passphrase="", replace=True, version=""):
    """Put an archive back, after taking a safety copy of what is here now."""
    data = read_archive(dest, name, passphrase)
    safety = None
    try:
        safety_dir = os.path.join(dest, "before-restore")
        os.makedirs(safety_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        safety = os.path.join(safety_dir, f"couchelephant-before-{stamp}.zip")
        with open(safety, "wb") as f:
            f.write(build(raw_db=True, with_secrets=True, version=version,
                          note="taken automatically before a restore"))
    except Exception:
        # A safety copy that cannot be written must not stop the restore the
        # user asked for; it is said in the answer instead.
        safety = None
    report = portable.import_bytes(data, replace=replace, include_secrets=True)
    report["safety_copy"] = os.path.basename(safety) if safety else None
    return report
