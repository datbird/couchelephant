"""Pull the guide and the DVR state from Plex into SQLite."""
import os
import time
import traceback

from . import db
from .plex import Plex, discover, PlexError


LOGO_DIR = os.environ.get("COUCHELEPHANT_LOGOS", "/data/logos")


def _now():
    return int(time.time())


def _airing_id(guid, media):
    """Stable per-broadcast id. Plex's own Media id is not always present, so
    the channel and start time identify the broadcast instead."""
    mid = media.get("id")
    if mid:
        return f"{guid}#{mid}"
    return f"{guid}#{media.get('channelIdentifier')}@{media.get('beginsAt')}"


def _upsert_program(c, m, section, now):
    teams = [{"id": t.get("id"), "name": t.get("tag")} for t in (m.get("Team") or [])]
    genres = [g.get("tag") for g in (m.get("Genre") or [])]
    c.execute(
        """INSERT INTO programs (guid, rating_key, title, grandparent_title, summary, type,
                                 section, genres, teams, thumb, art, originally_available,
                                 year, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(guid) DO UPDATE SET
             rating_key=excluded.rating_key, title=excluded.title,
             grandparent_title=excluded.grandparent_title, summary=excluded.summary,
             section=excluded.section, genres=excluded.genres, teams=excluded.teams,
             thumb=excluded.thumb, art=excluded.art,
             originally_available=excluded.originally_available,
             year=excluded.year, updated_at=excluded.updated_at""",
        (m.get("guid"), m.get("ratingKey"), m.get("title"), m.get("grandparentTitle"),
         m.get("summary"), m.get("type"), section, db.js(genres), db.js(teams),
         m.get("thumb") or m.get("grandparentThumb"), m.get("art"),
         m.get("originallyAvailableAt"), m.get("year"), now),
    )


def _upsert_channel(c, med, now):
    """Channel identity, including the Gracenote logo Plex points at."""
    vcn = med.get("channelVcn")
    if not vcn:
        return
    c.execute(
        """INSERT INTO channels (vcn, call_sign, title, network, identifier,
                                 thumb_url, updated_at)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(vcn) DO UPDATE SET
             call_sign=COALESCE(excluded.call_sign, channels.call_sign),
             title=COALESCE(excluded.title, channels.title),
             network=COALESCE(excluded.network, channels.network),
             identifier=COALESCE(excluded.identifier, channels.identifier),
             thumb_url=COALESCE(excluded.thumb_url, channels.thumb_url),
             updated_at=excluded.updated_at""",
        (vcn, med.get("channelCallSign"), med.get("channelTitle"),
         network_of(med.get("channelTitle")),
         med.get("channelIdentifier"), med.get("channelThumb"), now),
    )


def _upsert_airings(c, m, now):
    guid = m.get("guid")
    for med in (m.get("Media") or []):
        _upsert_channel(c, med, now)
        c.execute(
            """INSERT INTO airings (id, program_guid, channel_vcn, channel_call_sign,
                                    channel_identifier, channel_title, begins_at, ends_at,
                                    premiere, resolution, drm, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET
                 channel_vcn=excluded.channel_vcn, channel_call_sign=excluded.channel_call_sign,
                 channel_identifier=excluded.channel_identifier,
                 channel_title=excluded.channel_title, begins_at=excluded.begins_at,
                 ends_at=excluded.ends_at, premiere=excluded.premiere,
                 resolution=excluded.resolution, drm=excluded.drm,
                 updated_at=excluded.updated_at""",
            (_airing_id(guid, med), guid, med.get("channelVcn"), med.get("channelCallSign"),
             med.get("channelIdentifier"), med.get("channelTitle"),
             int(med.get("beginsAt") or 0) or None, int(med.get("endsAt") or 0) or None,
             1 if str(med.get("premiere") or "0") == "1" else 0,
             med.get("videoResolution"), 1 if med.get("drm") else 0, now),
        )


def sync_guide(plex, provider, shows, sports, movies=None):
    """Pull every section. Each has its own item type: shows and sports are
    episodes (type 4), movies are type 1. Query a section with the wrong type
    and it returns nothing, which is how 16 channels came to look empty."""
    now = _now()
    counts = {"programs": 0, "airings": 0}
    with db.tx() as c:
        for section, label, itype in ((shows, "shows", 4), (sports, "sports", 4),
                                      (movies, "movies", 1)):
            if not section:
                continue
            for m in plex.section_all(provider, section, type=itype):
                _upsert_program(c, m, label, now)
                _upsert_airings(c, m, now)
                counts["programs"] += 1
                counts["airings"] += len(m.get("Media") or [])
        # Drop anything that fell out of the guide window.
        c.execute("DELETE FROM airings WHERE updated_at < ?", (now,))
        c.execute("DELETE FROM programs WHERE guid NOT IN (SELECT program_guid FROM airings)")
    return counts


LOGO_MAX_AGE = 30 * 86400   # re-check a logo roughly monthly
LOGO_MAX_TRIES = 5          # give up on a persistently broken URL, but retry after MAX_AGE


def cache_logos(force=False):
    """Keep a local logo for every channel, and keep it correct.

    Runs on every sync, not once. A channel is re-fetched when any of these
    is true, so the cache repairs itself rather than going stale:
      - it has never been fetched
      - the file has gone missing from disk
      - Plex now points at a different URL than the one we stored
      - the copy is older than LOGO_MAX_AGE
    """
    import httpx
    os.makedirs(LOGO_DIR, exist_ok=True)
    now = _now()
    rows = db.query("SELECT * FROM channels WHERE thumb_url IS NOT NULL AND thumb_url != ''")

    todo = []
    for r in rows:
        path = r["logo_path"]
        reason = None
        if force:
            reason = "forced"
        elif not path:
            reason = "never fetched"
        elif not os.path.exists(path):
            reason = "file missing"
        elif (r["logo_source"] or "") != r["thumb_url"]:
            reason = "upstream url changed"
        elif now - (r["logo_fetched_at"] or 0) > LOGO_MAX_AGE:
            reason = "stale"
        if not reason:
            continue
        # Back off a URL that keeps failing, but let the age check retry it later.
        if (r["logo_attempts"] or 0) >= LOGO_MAX_TRIES and reason == "never fetched":
            continue
        todo.append((r, reason))

    fetched = failed = 0
    with httpx.Client(timeout=30.0, follow_redirects=True) as c:
        for r, reason in todo:
            safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in r["vcn"])
            path = os.path.join(LOGO_DIR, f"{safe}.png")
            try:
                resp = c.get(r["thumb_url"])
                if resp.status_code != 200 or not resp.content:
                    raise ValueError(f"HTTP {resp.status_code}")
                tmp = path + ".part"
                with open(tmp, "wb") as fh:
                    fh.write(resp.content)
                os.replace(tmp, path)   # never leave a half-written logo in place
            except Exception:
                failed += 1
                with db.tx() as conn:
                    conn.execute(
                        "UPDATE channels SET logo_attempts = COALESCE(logo_attempts,0) + 1 "
                        "WHERE vcn = ?", (r["vcn"],))
                continue
            fetched += 1
            with db.tx() as conn:
                conn.execute(
                    "UPDATE channels SET logo_path = ?, logo_source = ?, "
                    "logo_fetched_at = ?, logo_attempts = 0 WHERE vcn = ?",
                    (path, r["thumb_url"], now, r["vcn"]))
    return fetched, failed, len(todo)


def logo_coverage():
    total = db.one("SELECT COUNT(*) n FROM channels")["n"]
    have = db.one("SELECT COUNT(*) n FROM channels "
                  "WHERE logo_path IS NOT NULL AND logo_path != ''")["n"]
    none_offered = db.one("SELECT COUNT(*) n FROM channels "
                          "WHERE thumb_url IS NULL OR thumb_url = ''")["n"]
    return {"channels": total, "with_logo": have, "no_logo_upstream": none_offered}


def enrich_sports(plex, provider):
    """Fill in per-programme team tags for sports.

    A bulk section listing does not carry the Team array, so each sports
    programme is fetched once. Rows that already have teams are skipped, so
    this costs a burst on first run and almost nothing afterwards.
    """
    rows = db.query(
        "SELECT guid, rating_key FROM programs "
        "WHERE section = 'sports' AND (teams IS NULL OR teams = '[]')")
    now = _now()
    done = 0
    for r in rows:
        if not r["rating_key"]:
            continue
        try:
            m = plex.metadata(provider, r["rating_key"])
        except Exception:
            continue
        if not m:
            continue
        teams = [{"id": t.get("id"), "name": t.get("tag")} for t in (m.get("Team") or [])]
        if not teams:
            continue
        with db.tx() as c:
            c.execute("UPDATE programs SET teams = ?, updated_at = ? WHERE guid = ?",
                      (db.js(teams), now, r["guid"]))
        done += 1
    return done


def sync_teams(plex, provider, sports):
    if not sports:
        return 0
    now = _now()
    rows = plex.teams(provider, sports)
    with db.tx() as c:
        for t in rows:
            c.execute(
                "INSERT INTO teams (id, name, updated_at) VALUES (?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, updated_at=excluded.updated_at",
                (int(t.get("key")), t.get("title"), now),
            )
        c.execute("DELETE FROM teams WHERE updated_at < ?", (now,))
    return len(rows)


def network_of(title):
    """The network a channel carries.

    The guide names a channel `"41.1 KQGGDT (NBC)"`, and that trailing
    parenthetical is the only affiliation Plex exposes. The tuner's own channel
    list carries none, so this is the source.
    """
    if not title:
        return None
    a = title.rfind("(")
    b = title.rfind(")")
    if a == -1 or b < a:
        return None
    name = title[a + 1:b].strip()
    return name or None


def sync_channels(plex, dvr_key=None):
    """Kept for the DVR's channel identifiers. Names and logos come from the
    guide, which carries better data than the tuner's channel mapping."""
    now = _now()
    n = 0
    with db.tx() as c:
        for dvr in plex.dvrs():
            for dev in (dvr.get("Device") or []):
                for ch in (dev.get("ChannelMapping") or []):
                    vcn = ch.get("deviceIdentifier")
                    if not vcn:
                        continue
                    c.execute(
                        "INSERT INTO channels (vcn, identifier, updated_at) VALUES (?,?,?) "
                        "ON CONFLICT(vcn) DO UPDATE SET "
                        "identifier=excluded.identifier, updated_at=excluded.updated_at",
                        (vcn, ch.get("channelKey"), now))
                    n += 1
    return n


def sync_recordings(plex):
    """Mirror Plex's subscriptions and scheduled grabs.

    Subscriptions include the recurring kind ("All new episodes of X", team
    passes), which is what makes this view worth having.
    """
    now = _now()
    subs = plex.subscriptions()
    ours = {r["plex_subscription"] for r in db.query(
        "SELECT DISTINCT plex_subscription FROM pass_actions WHERE plex_subscription IS NOT NULL")}
    with db.tx() as c:
        for s in subs:
            key = str(s.get("key"))
            detail = plex.subscription(key) or s
            settings = {st.get("id"): st.get("value") for st in (detail.get("Setting") or [])}
            # Plex titles a rule by its template, "All Episodes", which says
            # nothing about what it follows. The programme is on the body, under
            # Directory for a series rule and Video for a single event.
            body = (detail.get("Directory") or detail.get("Video")
                    or s.get("Directory") or s.get("Video") or {})
            if isinstance(body, list):
                body = body[0] if body else {}
            target = body.get("grandparentTitle") or body.get("title") or None
            c.execute(
                """INSERT INTO plex_subscriptions (key, title, type, target, target_section,
                                                   settings, created_at, updated_at, owned_by_us)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(key) DO UPDATE SET title=excluded.title, type=excluded.type,
                     target=COALESCE(excluded.target, plex_subscriptions.target),
                     target_section=excluded.target_section, settings=excluded.settings,
                     updated_at=excluded.updated_at, owned_by_us=excluded.owned_by_us""",
                (key, s.get("title"), str(s.get("type")), target,
                 str(s.get("targetLibrarySectionID")),
                 db.js(settings), s.get("createdAt"), now, 1 if key in ours else 0),
            )
        c.execute("DELETE FROM plex_subscriptions WHERE updated_at < ?", (now,))

        for op in plex.scheduled():
            meta = op.get("Metadata") or op.get("Video") or {}
            media = meta.get("Media")
            if isinstance(media, dict):
                media = [media]
            # Plex returns mediaIndex as a string on some payloads and an int on
            # others, so coerce before using it as an index.
            try:
                idx = int(op.get("mediaIndex") or 0)
            except (TypeError, ValueError):
                idx = 0
            pool = media or [{}]
            chosen = pool[idx] if 0 <= idx < len(pool) else pool[0]
            c.execute(
                """INSERT INTO plex_grabs (id, subscription, status, title, parent_title,
                                           channel_vcn, begins_at, ends_at, updated_at)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET status=excluded.status,
                     channel_vcn=excluded.channel_vcn, begins_at=excluded.begins_at,
                     ends_at=excluded.ends_at, updated_at=excluded.updated_at""",
                (op.get("id") or f"{meta.get('guid')}#{idx}",
                 str(op.get("mediaSubscriptionID") or ""), op.get("status"),
                 meta.get("title"), meta.get("grandparentTitle"),
                 chosen.get("channelVcn"),
                 int(chosen.get("beginsAt") or 0) or None,
                 int(chosen.get("endsAt") or 0) or None, now),
            )
        c.execute("DELETE FROM plex_grabs WHERE updated_at < ?", (now,))

        # Attribution. Matching on the pass_actions subscription key alone
        # missed every recording, because the key Plex mints on create was
        # never captured. our_grabs is the reliable record: it holds the exact
        # channel and start time we asked for, and that pair identifies one
        # broadcast.
        c.execute(
            """UPDATE plex_subscriptions SET owned_by_us = 1 WHERE key IN (
                   SELECT g.subscription FROM plex_grabs g
                   JOIN our_grabs o ON o.begins_at = g.begins_at
                                   AND o.channel_vcn = g.channel_vcn
                   WHERE g.subscription IS NOT NULL AND g.subscription != '')""")
    return len(subs)


def full_sync():
    """One pass over everything. Returns a short human-readable summary."""
    started = _now()
    detail = ""
    ok = 0
    try:
        plex = Plex(db.get_setting("plex_url"), db.get_setting("plex_token"))
        provider, shows, sports, movies = discover(plex)
        db.set_setting("epg_provider", provider)
        db.set_setting("shows_section", shows or "")
        db.set_setting("sports_section", sports or "")
        db.set_setting("movies_section", movies or "")

        chans = sync_channels(plex)
        teams = sync_teams(plex, provider, sports)
        guide = sync_guide(plex, provider, shows, sports, movies)
        enriched = enrich_sports(plex, provider)
        got, bad, tried = cache_logos()
        subs = sync_recordings(plex)

        cov = logo_coverage()
        nch = cov["channels"]
        detail = (f"{guide['programs']} programs, {guide['airings']} airings, "
                  f"{teams} teams, {enriched} sports enriched, "
                  f"{nch} channels, logos {cov['with_logo']}/{cov['channels']}"
                  + (f" (+{got} fetched)" if got else "")
                  + (f" ({bad} failed)" if bad else "")
                  + f", {subs} subscriptions")
        ok = 1
    except Exception as e:
        # Keep the frame that actually failed. A bare type+message sent me
        # chasing the wrong module once already.
        tb = traceback.extract_tb(e.__traceback__)
        where = " <- ".join(f"{f.name}:{f.lineno}" for f in reversed(tb[-4:]))
        detail = f"{type(e).__name__}: {e} [{where}]"
    with db.tx() as c:
        c.execute("INSERT INTO sync_log (started_at, ended_at, ok, detail) VALUES (?,?,?,?)",
                  (started, _now(), ok, detail))
        c.execute("DELETE FROM sync_log WHERE id NOT IN "
                  "(SELECT id FROM sync_log ORDER BY id DESC LIMIT 50)")
    return ok, detail
