"""Telling somebody, outside this app, that something happened.

CouchElephant already detects the failure that matters. On 2026-08-30 it opened
an `epg_stale` notice, correctly, and nobody saw it for four days, because a
notice only exists on a page nobody had open. Detection without delivery is not
monitoring.

So this sends. Outbound only, and deliberately so: a self-hosted install sits
behind a router with no public address, so nothing here may ever need a port
forwarded or a socket held open.

  - **Discord needs no bot.** A webhook URL is the entire integration.
  - **Telegram needs a token but no running bot.** One HTTPS POST. "Bot" is
    only what Telegram calls the token.
  - **Notifiarr** is a relay somebody may already run. It costs one more hop,
    and it buys one bot in one channel instead of a webhook per application.
    Whoever already routes Radarr, Sonarr and Plex through it wants this here
    too, rather than a fifteenth integration to set up and remember.

Two kinds of thing are worth sending, and they behave differently.

A **fault** has a life. It opens, it stays open, it clears. Saying it once and
never again means a problem that started while you were away is a problem you
never hear about; saying it every sync means an hourly duplicate and a muted
channel. So: once when it opens, a reminder on a schedule while it stays open,
once when it clears.

An **activity** event is a moment. A booking was made, a recording started. It
is said exactly once, ever.

`notify_state` carries both, on one rule: **a row exists means this destination
has heard about this thing.** `last_sent_at IS NULL` is the interesting case: it
means the row was seeded when the destination was created, so the destination
knows about the thing but was never told. That is what stops a channel added
today being handed a backlog of everything already open, and it is also why such
a fault sends no recovery message either. You cannot be told a problem ended if
you were never told it started.
"""
import time
import uuid
from urllib.parse import urlparse

import httpx

from . import db, health

TIMEOUT = 10.0

# Telegram has no user-supplied host: the URL is built from this and the token,
# so there is no server-side request forgery surface on that side at all.
TELEGRAM_BASE = "https://api.telegram.org"

# Notifiarr's passthrough. Also no user-supplied host: the key goes in the path
# and the Discord channel goes in the body, so there is no forgery surface here
# either.
NOTIFIARR_BASE = "https://notifiarr.com"

# Discord's webhook URL *is* user-supplied, and the server will POST to it. Left
# unchecked that is an SSRF hole pointed at the LAN this container sits on. The
# host is pinned. `DISCORD_HOSTS` is the one the app uses and is relaxed by the
# suite to reach its local fake; `REAL_DISCORD_HOSTS` is what production means
# and is what the validation test asserts against.
REAL_DISCORD_HOSTS = frozenset({
    "discord.com", "discordapp.com", "ptb.discord.com", "canary.discord.com",
})
DISCORD_HOSTS = REAL_DISCORD_HOSTS
# The scheme is the other half of the same policy. Plain http would send the
# webhook URL, which is a bearer credential, over the wire in clear.
REAL_DISCORD_SCHEMES = frozenset({"https"})
DISCORD_SCHEMES = REAL_DISCORD_SCHEMES

# ---------- the catalog ----------

# Faults come from health.py, which already carries severity, title, detail and
# the dates. Nothing new is needed to produce them.
FAULTS = [
    (health.EPG_REFRESH_OFF, "Plex's guide refresh is switched off"),
    (health.EPG_STALE, "Plex has not refreshed its guide"),
    (health.GUIDE_SHORT, "The guide is running out"),
    (health.PLEX_UNREACHABLE, "Plex did not answer"),
    (health.TEAM_PASS_UNMATCHED, "A team you follow has no games in the guide"),
    (health.BOOKING_DRIFT, "A booking no longer matches its pass"),
    (health.BOOKING_REPAIR_FAILED, "A booking could not be repaired"),
    (health.EXPECTATION_MISSED, "The guide passed a date without the show"),
]

PASS_BOOKED = "pass_booked"
RECORDING_STARTED = "recording_started"
RECORDING_FINISHED = "recording_finished"
SYNC_FAILED = "sync_failed"

ACTIVITY = [
    (PASS_BOOKED, "A pass booked a recording"),
    (RECORDING_STARTED, "A recording started"),
    (RECORDING_FINISHED, "A recording finished"),
    (SYNC_FAILED, "A sync failed"),
]

FAULT_CODES = frozenset(c for c, _ in FAULTS)
ACTIVITY_CODES = frozenset(c for c, _ in ACTIVITY)
CATALOG = FAULTS + ACTIVITY
CATALOG_CODES = FAULT_CODES | ACTIVITY_CODES

# `keys_available` is deliberately absent. It is a `tip`, the only dismissible
# severity, and a suggestion is never worth interrupting somebody for. Asking
# for it by name does not get it: the severity is checked again at send time.

# Only `complete` was ever observed on a live server, so nothing may depend on
# knowing Plex's other status words. Anything outside this set is read as still
# going, which costs a late message at worst and never a missed one.
TERMINAL = frozenset({"complete", "completed", "error", "failed",
                      "cancelled", "canceled", "aborted"})

# A moment older than this is not news. Without it, pruning a state row would
# let its event be announced all over again, which is a loop rather than a bug
# you notice once.
ACTIVITY_WINDOW = 7 * 86400
# And state for a moment is kept four times longer than the window that could
# re-announce it, so the two can never meet.
STATE_TTL = 30 * 86400

KINDS = ("discord", "telegram", "notifiarr")

SEVERITY_COLOUR = {"bad": 0xD1453B, "warn": 0xD9822B, "ok": 0x3BA55D}


def _now() -> int:
    return int(time.time())


# ---------- destinations ----------

def valid_webhook(url: str, hosts=None, schemes=None) -> bool:
    """Is this a Discord webhook, and only that?

    Refuses anything that is not https, and anything whose host is not Discord's.
    `discord.com.evil.example.com` fails because the host is compared whole and
    never with `endswith`, which is the mistake this check exists to avoid.

    Both halves are parameters so the suite can point at a local fake without
    the app carrying a branch for it. Production passes neither.
    """
    try:
        u = urlparse((url or "").strip())
    except ValueError:
        return False
    if not u.hostname:
        return False
    if u.scheme not in (schemes if schemes is not None else DISCORD_SCHEMES):
        return False
    return u.hostname.lower() in (hosts if hosts is not None else DISCORD_HOSTS)


def _mask(v) -> str:
    return "*" * 12 if v else ""


def destinations() -> list[dict]:
    """Every destination, safe to render. Secrets are masked here rather than in
    the template, so a new template cannot leak one by forgetting to."""
    out = []
    for r in db.query("SELECT * FROM destinations ORDER BY id"):
        d = dict(r)
        d["webhook"] = _mask(d.get("webhook"))
        d["token"] = _mask(d.get("token"))
        d["event_list"] = split_events(d.get("events"))
        d["event_count"] = len(d["event_list"])
        out.append(d)
    return out


def get_destination(dest_id: int) -> dict | None:
    """One destination, secrets included. Internal use only: this must never be
    handed to a template. `destinations()` is the one that is safe to render."""
    r = db.one("SELECT * FROM destinations WHERE id = ?", (dest_id,))
    return dict(r) if r else None


def split_events(events) -> list[str]:
    """The stored comma-separated codes, as a list. Public because the settings
    route needs it to round-trip a destination it is only half-editing."""
    return [e for e in (events or "").split(",") if e]


def save_destination(*, name, kind, events, remind_hours=24, webhook=None,
                     token=None, chat_id=None, dest_id=None, hosts=None,
                     schemes=None) -> int:
    """Create or update one destination. Returns its id.

    A brand new destination is seeded with everything that is already true, so
    it starts from now rather than being handed a backlog. See `_seed`.
    """
    kind = (kind or "").strip().lower()
    if kind not in KINDS:
        raise ValueError(f"unknown destination kind: {kind!r}")
    name = (name or "").strip() or kind.title()
    codes = [e for e in events if e in CATALOG_CODES]
    if kind == "discord":
        if webhook and not valid_webhook(webhook, hosts=hosts, schemes=schemes):
            raise ValueError("That is not a Discord webhook URL.")
    now = _now()
    with db.tx() as c:
        if dest_id:
            # An empty secret means "leave the one you have". The UI sends dots
            # rather than the real value, and a save must not wipe it.
            sets, params = ["name=?", "kind=?", "events=?", "remind_hours=?",
                            "updated_at=?"], [name, kind, ",".join(codes),
                                              int(remind_hours or 0), now]
            for col, val in (("webhook", webhook), ("token", token),
                             ("chat_id", chat_id)):
                if val:
                    sets.append(f"{col}=?")
                    params.append(val)
            c.execute(f"UPDATE destinations SET {','.join(sets)} WHERE id = ?",
                      (*params, dest_id))
            return dest_id
        cur = c.execute(
            "INSERT INTO destinations (uid, name, kind, webhook, token, chat_id, "
            "events, remind_hours, enabled, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,1,?,?)",
            (uuid.uuid4().hex, name, kind, webhook, token, chat_id,
             ",".join(codes), int(remind_hours or 0), now, now))
        dest_id = cur.lastrowid
    _seed(dest_id, now)
    return dest_id


def delete_destination(dest_id: int) -> None:
    with db.tx() as c:
        c.execute("DELETE FROM notify_state WHERE destination_id = ?", (dest_id,))
        c.execute("DELETE FROM destinations WHERE id = ?", (dest_id,))


def set_enabled(dest_id: int, on: bool) -> None:
    with db.tx() as c:
        c.execute("UPDATE destinations SET enabled = ?, updated_at = ? WHERE id = ?",
                  (1 if on else 0, _now(), dest_id))


def _seed(dest_id: int, now: int) -> None:
    """Record everything already true, without announcing any of it.

    `last_sent_at` stays NULL, which is what marks these as known-but-untold. A
    fault seeded this way sends no recovery either, because a channel cannot be
    told a problem ended when it was never told the problem started.
    """
    rows = [(r["code"], r["code"]) for r in
            db.query("SELECT code FROM notices WHERE resolved_at IS NULL")]
    rows += [(PASS_BOOKED, r["airing_id"]) for r in
             db.query("SELECT airing_id FROM our_grabs")]
    rows += [(RECORDING_STARTED, r["id"]) for r in
             db.query("SELECT id FROM plex_grabs WHERE begins_at <= ?", (now,))]
    rows += [(SYNC_FAILED, str(r["id"])) for r in
             db.query("SELECT id FROM sync_log WHERE ok = 0")]
    if not rows:
        return
    with db.tx() as c:
        c.executemany(
            "INSERT OR IGNORE INTO notify_state (destination_id, event, key, "
            "opened_at, last_sent_at) VALUES (?,?,?,?,NULL)",
            [(dest_id, ev, key, now) for ev, key in rows])


# ---------- transports ----------

class SendError(RuntimeError):
    pass


def _send_discord(dest, title, detail, severity) -> None:
    """An embed, coloured by severity. Discord answers 204 with no body."""
    payload = {"embeds": [{
        "title": title[:256],
        "description": (detail or "")[:4000],
        "color": SEVERITY_COLOUR.get(severity, SEVERITY_COLOUR["warn"]),
    }]}
    with httpx.Client(timeout=TIMEOUT) as http:
        r = http.post(dest["webhook"], json=payload)
    if r.status_code >= 400:
        raise SendError(f"Discord answered {r.status_code}")


def _send_telegram(dest, title, detail, severity) -> None:
    """Plain text. Telegram answers 200 with `ok: false` for a refusal, so the
    status code alone never proves a message was delivered."""
    mark = {"bad": "[BAD]", "warn": "[WARN]"}.get(severity, "[OK]")
    text = f"{mark} {title}"
    if detail:
        text += f"\n{detail}"
    url = f"{TELEGRAM_BASE.rstrip('/')}/bot{dest['token']}/sendMessage"
    with httpx.Client(timeout=TIMEOUT) as http:
        r = http.post(url, json={"chat_id": dest.get("chat_id"),
                                 "text": text[:4000],
                                 "disable_web_page_preview": True})
    if r.status_code >= 400:
        raise SendError(f"Telegram answered {r.status_code}")
    body = r.json() if r.content else {}
    if not body.get("ok"):
        raise SendError(f"Telegram refused it: {body.get('description') or 'no reason given'}")


def _send_notifiarr(dest, title, detail, severity) -> None:
    """Passthrough to notifiarr.com, which posts it into the Discord channel.

    The reply is text rather than JSON on some paths, so success is judged on
    the status code and on the body not announcing an error, rather than on a
    field that is not always there.
    """
    payload = {
        "notification": {"update": False, "name": "CouchElephant"},
        "discord": {
            "ids": {"channel": int(dest["chat_id"])},
            "color": f"{SEVERITY_COLOUR.get(severity, SEVERITY_COLOUR['warn']):06X}",
            "text": {"title": title[:256], "description": (detail or "")[:4000]},
        },
    }
    url = f"{NOTIFIARR_BASE.rstrip('/')}/api/v1/notification/passthrough/{dest['token']}"
    with httpx.Client(timeout=TIMEOUT) as http:
        r = http.post(url, json=payload,
                      headers={"Content-Type": "application/json",
                               "Accept": "text/plain"})
    if r.status_code >= 400:
        raise SendError(f"Notifiarr answered {r.status_code}")
    body = (r.text or "").strip()
    if body.lower().startswith("error") or '"result":"error"' in body.replace(" ", ""):
        raise SendError(f"Notifiarr refused it: {body[:200]}")


def _deliver(dest, title, detail, severity) -> bool:
    """One message, one destination. Never raises.

    A notification that cannot be delivered must not break a sync, and it must
    not be recorded as delivered either: a failure returns False, no state row
    is written, and the next sync tries again. One blip must not lose the alert.
    """
    try:
        if dest["kind"] == "discord":
            if not dest.get("webhook"):
                raise SendError("No webhook URL is set.")
            _send_discord(dest, title, detail, severity)
        elif dest["kind"] == "telegram":
            if not (dest.get("token") and dest.get("chat_id")):
                raise SendError("The token or the chat is missing.")
            _send_telegram(dest, title, detail, severity)
        elif dest["kind"] == "notifiarr":
            if not (dest.get("token") and dest.get("chat_id")):
                raise SendError("The API key or the channel is missing.")
            if not str(dest["chat_id"]).strip().isdigit():
                raise SendError("The channel must be a numeric Discord channel id.")
            _send_notifiarr(dest, title, detail, severity)
        else:
            raise SendError(f"Unknown kind {dest['kind']!r}")
    except Exception as e:
        _mark(dest["id"], ok=False, error=f"{type(e).__name__}: {e}"
              if not isinstance(e, SendError) else str(e))
        return False
    _mark(dest["id"], ok=True, error=None)
    return True


def _mark(dest_id, *, ok, error) -> None:
    with db.tx() as c:
        if ok:
            c.execute("UPDATE destinations SET last_ok_at = ?, last_error = NULL "
                      "WHERE id = ?", (_now(), dest_id))
        else:
            c.execute("UPDATE destinations SET last_error = ? WHERE id = ?",
                      (error, dest_id))


def test(dest_id: int) -> str:
    """The Send a test message button. Returns a verdict the UI shows as-is."""
    dest = get_destination(dest_id)
    if not dest:
        return "That destination no longer exists."
    try:
        if dest["kind"] == "discord":
            if not dest.get("webhook"):
                return "No webhook URL is set."
            _send_discord(dest, "CouchElephant test",
                          "If you can read this, alerts will reach here.", "ok")
        elif dest["kind"] == "notifiarr":
            if not dest.get("token"):
                return "No Notifiarr API key is set."
            if not str(dest.get("chat_id") or "").strip().isdigit():
                return ("No Discord channel id is set. Turn on Developer Mode in "
                        "Discord, right-click the channel, Copy Channel ID.")
            _send_notifiarr(dest, "CouchElephant test",
                            "If you can read this, alerts will reach here.", "ok")
        else:
            if not dest.get("token"):
                return "No bot token is set."
            if not dest.get("chat_id"):
                return "No chat is set. Message the bot, then press Find chat."
            _send_telegram(dest, "CouchElephant test",
                           "If you can read this, alerts will reach here.", "ok")
    except Exception as e:
        _mark(dest_id, ok=False, error=str(e))
        return f"Failed: {e}"
    _mark(dest_id, ok=True, error=None)
    return f"OK, sent to {dest['name']}."


def find_chat_id(token: str) -> str | None:
    """Read the chat id off the first update, so nobody hunts for a number.

    Returns None when the bot has never been messaged, which is the normal
    first answer and not an error.
    """
    url = f"{TELEGRAM_BASE.rstrip('/')}/bot{token}/getUpdates"
    try:
        with httpx.Client(timeout=TIMEOUT) as http:
            r = http.post(url, json={"limit": 10})
        body = r.json() if r.content else {}
    except Exception:
        return None
    if not body.get("ok"):
        return None
    for u in body.get("result") or []:
        msg = u.get("message") or u.get("channel_post") or {}
        cid = (msg.get("chat") or {}).get("id")
        if cid is not None:
            return str(cid)
    return None


# ---------- state ----------

def _state(dest_id, event, key):
    return db.one("SELECT * FROM notify_state WHERE destination_id = ? AND "
                  "event = ? AND key = ?", (dest_id, event, key))


def _record(dest_id, event, key, now, opened_at=None) -> None:
    with db.tx() as c:
        c.execute(
            "INSERT INTO notify_state (destination_id, event, key, opened_at, "
            "last_sent_at) VALUES (?,?,?,?,?) "
            "ON CONFLICT(destination_id, event, key) DO UPDATE SET "
            "last_sent_at = excluded.last_sent_at",
            (dest_id, event, key, opened_at or now, now))


def _forget(dest_id, event, key) -> None:
    with db.tx() as c:
        c.execute("DELETE FROM notify_state WHERE destination_id = ? AND "
                  "event = ? AND key = ?", (dest_id, event, key))


def _prune(now: int) -> None:
    """Drop state for moments long past. Faults keep theirs for as long as they
    are open, however long that is."""
    marks = ",".join("?" * len(ACTIVITY_CODES))
    with db.tx() as c:
        c.execute(
            f"DELETE FROM notify_state WHERE event IN ({marks}) AND "
            f"COALESCE(last_sent_at, opened_at) < ?",
            (*sorted(ACTIVITY_CODES), now - STATE_TTL))


# ---------- dispatch ----------

def dispatch(now: int | None = None) -> int:
    """Send whatever each destination has not been told. Returns messages sent.

    Called at the end of `full_sync`, after the sync_log row is written, so a
    failed sync can see its own row.
    """
    now = now or _now()
    sent = 0
    rows = db.query("SELECT * FROM destinations WHERE enabled = 1")
    if rows:
        for r in rows:
            dest = dict(r)
            wanted = set(split_events(dest.get("events"))) & CATALOG_CODES
            if not wanted:
                continue
            sent += _faults(dest, wanted, now)
            sent += _activity(dest, wanted, now)
    _prune(now)
    return sent


def _faults(dest, wanted, now) -> int:
    sent = 0
    codes = wanted & FAULT_CODES
    if not codes:
        return 0
    marks = ",".join("?" * len(codes))
    for n in db.query(f"SELECT * FROM notices WHERE code IN ({marks})",
                      tuple(sorted(codes))):
        # Checked again here and not only at the catalog: a severity can change
        # under a code, and a tip must never send whatever it is asked.
        if n["severity"] == health.TIP:
            continue
        st = _state(dest["id"], n["code"], n["code"])
        if n["resolved_at"] is None:
            if st is None:
                if _deliver(dest, n["title"], n["detail"], n["severity"]):
                    _record(dest["id"], n["code"], n["code"], now,
                            opened_at=n["first_seen"])
                    sent += 1
            elif st["last_sent_at"] is not None:
                every = int(dest.get("remind_hours") or 0) * 3600
                if every and now - st["last_sent_at"] >= every:
                    age = health._days(now - (st["opened_at"] or now))
                    if _deliver(dest, f"Still open: {n['title']}",
                                f"Open for {age}.\n{n['detail'] or ''}".strip(),
                                n["severity"]):
                        _record(dest["id"], n["code"], n["code"], now,
                                opened_at=st["opened_at"])
                        sent += 1
        elif st is not None:
            if st["last_sent_at"] is not None:
                age = health._days((n["resolved_at"] or now) - (st["opened_at"] or now))
                if not _deliver(dest, f"Cleared: {n['title']}",
                                f"Was open for {age}.", "ok"):
                    continue
                sent += 1
            _forget(dest["id"], n["code"], n["code"])
    return sent


def _activity(dest, wanted, now) -> int:
    sent = 0
    fresh = now - ACTIVITY_WINDOW

    if PASS_BOOKED in wanted:
        for g in db.query("SELECT * FROM our_grabs WHERE created_at >= ?", (fresh,)):
            if _state(dest["id"], PASS_BOOKED, g["airing_id"]):
                continue
            when = _clock(g["begins_at"])
            if _deliver(dest, "A pass booked a recording",
                        f"{g['title']} on {g['channel_vcn']}{when}", "ok"):
                _record(dest["id"], PASS_BOOKED, g["airing_id"], now)
                sent += 1

    if RECORDING_STARTED in wanted:
        for g in db.query("SELECT * FROM plex_grabs WHERE begins_at <= ? AND "
                          "begins_at >= ?", (now, fresh)):
            if _state(dest["id"], RECORDING_STARTED, g["id"]):
                continue
            if _deliver(dest, "Recording started",
                        f"{g['title']} on {g['channel_vcn']}", "ok"):
                _record(dest["id"], RECORDING_STARTED, g["id"], now)
                sent += 1

    if RECORDING_FINISHED in wanted:
        # Only something we said had started can finish. The started row stays
        # after this fires, so the same grab is not announced as starting all
        # over again; a separate finished row is what stops a second finish.
        for st in db.query("SELECT * FROM notify_state WHERE destination_id = ? "
                           "AND event = ? AND last_sent_at IS NOT NULL",
                           (dest["id"], RECORDING_STARTED)):
            key = st["key"]
            if _state(dest["id"], RECORDING_FINISHED, key):
                continue
            g = db.one("SELECT * FROM plex_grabs WHERE id = ?", (key,))
            # Gone from Plex's schedule, or reached a status that means done.
            done = g is None or (g["status"] or "").lower() in TERMINAL
            if not done:
                continue
            title = g["title"] if g else key
            if _deliver(dest, "Recording finished", str(title), "ok"):
                _record(dest["id"], RECORDING_FINISHED, key, now)
                sent += 1

    if SYNC_FAILED in wanted:
        for s in db.query("SELECT * FROM sync_log WHERE ok = 0 AND ended_at >= ?",
                          (fresh,)):
            key = str(s["id"])
            if _state(dest["id"], SYNC_FAILED, key):
                continue
            if _deliver(dest, "A sync failed", s["detail"] or "No detail.", "bad"):
                _record(dest["id"], SYNC_FAILED, key, now)
                sent += 1

    return sent


def _clock(ts) -> str:
    if not ts:
        return ""
    return time.strftime(" at %a %d %b %H:%M", time.localtime(int(ts)))
