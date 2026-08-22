"""App accounts: local username and password, plus Cloudflare Access.

Three modes, chosen in Settings:

  none        no sign-in at all, which is right for a box on a home LAN
  local       accounts held here, scrypt hashed, session cookie
  cloudflare  Cloudflare Access authenticates, we verify its signed token

Local passwords are scrypt hashed with a per-user random salt. Session tokens
are random and stored hashed, so a copy of the database grants no logins.

Everything lives beside the main database, in its own file, so a rebuild of the
guide cache can never take the accounts with it.

The shape of this is taken from the same author's ludodex, which solved it
first; the three-mode switch and the per-user theme are new here.
"""
import hashlib
import hmac
import os
import secrets
import sqlite3
import time

from . import db

DB_PATH = os.environ.get("COUCHELEPHANT_AUTH_DB",
                         os.path.join(os.path.dirname(db.DB_PATH), "auth.db"))

SESSION_TTL = 30 * 24 * 3600
SESSION_COOKIE = "ce_session"
MIN_PASSWORD = 8
MODES = ("none", "local", "cloudflare")

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    username   TEXT UNIQUE COLLATE NOCASE,
    pw_hash    TEXT,
    pw_salt    TEXT,
    role       TEXT DEFAULT 'admin',
    created_at INTEGER
);
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER,
    created_at INTEGER,
    expires_at INTEGER
);
-- A Cloudflare Access identity is an email. It is mapped to a local account so
-- preferences and roles work the same whichever way someone signed in.
CREATE TABLE IF NOT EXISTS email_map (
    email      TEXT PRIMARY KEY COLLATE NOCASE,
    user_id    INTEGER,
    created_at INTEGER
);
CREATE TABLE IF NOT EXISTS prefs (
    user_id INTEGER,
    key     TEXT,
    value   TEXT,
    PRIMARY KEY (user_id, key)
);
"""


def _con():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def _now():
    return int(time.time())


def _hash_pw(password, salt=None):
    salt = salt or secrets.token_hex(16)
    h = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt),
                       n=16384, r=8, p=1, dklen=32)
    return h.hex(), salt


def _tok_hash(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---------- mode ----------

def mode():
    m = db.get_setting("auth_mode") or "none"
    return m if m in MODES else "none"


def needs_setup():
    """True when sign-in is on but nobody has an account yet."""
    return mode() != "none" and user_count() == 0


# ---------- users ----------

def user_count():
    con = _con()
    try:
        return con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    finally:
        con.close()


def create_user(username, password, role="admin"):
    username = (username or "").strip()
    if not username:
        raise ValueError("a username is required")
    if len(password or "") < MIN_PASSWORD:
        raise ValueError(f"the password must be at least {MIN_PASSWORD} characters")
    ph, salt = _hash_pw(password)
    con = _con()
    try:
        con.execute("INSERT INTO users (username, pw_hash, pw_salt, role, created_at) "
                    "VALUES (?,?,?,?,?)", (username, ph, salt, role, _now()))
        con.commit()
        return con.execute("SELECT id FROM users WHERE username=? COLLATE NOCASE",
                           (username,)).fetchone()[0]
    except sqlite3.IntegrityError:
        raise ValueError("that username is taken")
    finally:
        con.close()


def verify(username, password):
    con = _con()
    try:
        row = con.execute("SELECT * FROM users WHERE username=? COLLATE NOCASE",
                          ((username or "").strip(),)).fetchone()
    finally:
        con.close()
    if not row:
        # Hash anyway, so a missing username does not answer faster than a
        # wrong password and give the difference away.
        _hash_pw(password or "", secrets.token_hex(16))
        return None
    ph, _ = _hash_pw(password or "", row["pw_salt"])
    if hmac.compare_digest(ph, row["pw_hash"]):
        return {"id": row["id"], "username": row["username"], "role": row["role"]}
    return None


def list_users():
    con = _con()
    try:
        return [dict(r) for r in con.execute(
            "SELECT id, username, role, created_at FROM users ORDER BY created_at")]
    finally:
        con.close()


def delete_user(uid):
    con = _con()
    try:
        con.execute("DELETE FROM users WHERE id=?", (uid,))
        con.execute("DELETE FROM sessions WHERE user_id=?", (uid,))
        con.execute("DELETE FROM email_map WHERE user_id=?", (uid,))
        con.execute("DELETE FROM prefs WHERE user_id=?", (uid,))
        con.commit()
    finally:
        con.close()


# ---------- sessions ----------

def create_session(user_id):
    token = secrets.token_urlsafe(32)
    now = _now()
    con = _con()
    try:
        con.execute("INSERT INTO sessions (token_hash, user_id, created_at, expires_at) "
                    "VALUES (?,?,?,?)", (_tok_hash(token), user_id, now, now + SESSION_TTL))
        con.execute("DELETE FROM sessions WHERE expires_at < ?", (now,))
        con.commit()
    finally:
        con.close()
    return token


def session_user(token):
    if not token:
        return None
    con = _con()
    try:
        row = con.execute(
            "SELECT u.id, u.username, u.role, s.expires_at FROM sessions s "
            "JOIN users u ON u.id = s.user_id WHERE s.token_hash=?",
            (_tok_hash(token),)).fetchone()
    finally:
        con.close()
    if not row or row["expires_at"] < _now():
        return None
    return {"id": row["id"], "username": row["username"], "role": row["role"]}


def delete_session(token):
    if not token:
        return
    con = _con()
    try:
        con.execute("DELETE FROM sessions WHERE token_hash=?", (_tok_hash(token),))
        con.commit()
    finally:
        con.close()


# ---------- Cloudflare Access ----------

def user_for_email(email, create=True):
    """The local account behind a Cloudflare identity.

    An unmapped email gets an account on first sight, because Cloudflare has
    already decided who may reach this app. Turning that off would mean adding
    every person by hand after Access had already let them in.
    """
    if not email:
        return None
    con = _con()
    try:
        row = con.execute(
            "SELECT u.id, u.username, u.role FROM email_map m "
            "JOIN users u ON u.id = m.user_id WHERE m.email=? COLLATE NOCASE",
            (email,)).fetchone()
        if row:
            return dict(row)
        if not create:
            return None
        first = con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        name = email.split("@")[0] or email
        base, n = name, 1
        while con.execute("SELECT 1 FROM users WHERE username=? COLLATE NOCASE",
                          (name,)).fetchone():
            n += 1
            name = f"{base}{n}"
        con.execute("INSERT INTO users (username, pw_hash, pw_salt, role, created_at) "
                    "VALUES (?,?,?,?,?)",
                    (name, "", "", "admin" if first else "user", _now()))
        uid = con.execute("SELECT id FROM users WHERE username=? COLLATE NOCASE",
                          (name,)).fetchone()[0]
        con.execute("INSERT OR REPLACE INTO email_map (email, user_id, created_at) "
                    "VALUES (?,?,?)", (email, uid, _now()))
        con.commit()
        return {"id": uid, "username": name,
                "role": "admin" if first else "user"}
    finally:
        con.close()


# ---------- per-user preferences ----------

def get_pref(user_id, key, default=None):
    if not user_id:
        return default
    con = _con()
    try:
        row = con.execute("SELECT value FROM prefs WHERE user_id=? AND key=?",
                          (user_id, key)).fetchone()
        return row["value"] if row else default
    finally:
        con.close()


def set_pref(user_id, key, value):
    if not user_id:
        return
    con = _con()
    try:
        con.execute("INSERT INTO prefs (user_id, key, value) VALUES (?,?,?) "
                    "ON CONFLICT(user_id, key) DO UPDATE SET value=excluded.value",
                    (user_id, key, value))
        con.commit()
    finally:
        con.close()
