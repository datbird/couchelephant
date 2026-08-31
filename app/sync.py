"""Pull the guide and the DVR state from Plex into SQLite."""
import os
import time
import traceback

from . import db, expectations, health, passes, teamcat, verify
from .plex import Plex, PlexError, discover

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


def _int_or_none(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _upsert_program(c, m, section, now):
    teams = [{"id": t.get("id"), "name": t.get("tag")} for t in (m.get("Team") or [])]
    genres = [g.get("tag") for g in (m.get("Genre") or [])]
    c.execute(
        """INSERT INTO programs (guid, rating_key, title, grandparent_title, summary, type,
                                 section, genres, teams, thumb, art, originally_available,
                                 year, content_rating, duration, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(guid) DO UPDATE SET
             rating_key=excluded.rating_key, title=excluded.title,
             grandparent_title=excluded.grandparent_title, summary=excluded.summary,
             section=excluded.section, genres=excluded.genres,
             -- A bulk section listing carries Genre but NOT Team, so an
             -- incoming empty list means "not asked", not "no teams". Writing
             -- it back blanked what enrich_sports had fetched, and made every
             -- sync re-fetch every sports programme.
             teams=CASE WHEN excluded.teams IN ('[]', '') OR excluded.teams IS NULL
                        THEN programs.teams ELSE excluded.teams END,
             thumb=excluded.thumb, art=excluded.art,
             originally_available=excluded.originally_available,
             year=excluded.year,
             -- Not every item carries a rating. An incoming blank means the
             -- guide did not say, not that the rating was withdrawn.
             content_rating=CASE WHEN excluded.content_rating IS NULL OR
                                      excluded.content_rating = ''
                                 THEN programs.content_rating
                                 ELSE excluded.content_rating END,
             duration=COALESCE(excluded.duration, programs.duration),
             updated_at=excluded.updated_at""",
        (m.get("guid"), m.get("ratingKey"), m.get("title"), m.get("grandparentTitle"),
         m.get("summary"), m.get("type"), section, db.js(genres), db.js(teams),
         m.get("thumb") or m.get("grandparentThumb"), m.get("art"),
         m.get("originallyAvailableAt"), m.get("year"), m.get("contentRating"),
         _int_or_none(m.get("duration")), now),
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


def sync_guide(plex: Plex, provider: str, shows, sports, movies=None) -> dict[str, int]:
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


def cache_logos(force: bool = False) -> tuple[int, int, int]:
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
        for r, _reason in todo:
            safe = "".join(ch if (ch.isalnum() and ch.isascii()) or ch in "._-" else "_" for ch in r["vcn"])
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


def logo_coverage() -> dict[str, int]:
    total = db.one("SELECT COUNT(*) n FROM channels")["n"]
    have = db.one("SELECT COUNT(*) n FROM channels "
                  "WHERE logo_path IS NOT NULL AND logo_path != ''")["n"]
    none_offered = db.one("SELECT COUNT(*) n FROM channels "
                          "WHERE thumb_url IS NULL OR thumb_url = ''")["n"]
    return {"channels": total, "with_logo": have, "no_logo_upstream": none_offered}


TEAMS_RETRY_AGE = 86400     # ask again about an untagged sports row daily


def enrich_sports(plex: Plex, provider: str) -> tuple[int, int]:
    """Fill in per-programme team tags for sports. Returns (asked, filled).

    A bulk section listing does not carry the Team array, so each sports
    programme is fetched once. Rows that already have teams are skipped, so
    this costs a burst on first run and almost nothing afterwards.

    Most of a real guide's sports section is not a game: a highlights show, a
    phone-in, a shop. Plex answers 200 for those and simply omits Team, so the
    row can never be filled in. Every attempt is therefore written down,
    whatever it found, or the row qualifies again on the next sync and the app
    asks the same seventy questions every hour for as long as it is on air.

    The note expires rather than settling it for good. A game can reach the
    guide before Plex tags it, and a permanent "no teams" would hide that game
    from a team pass for the rest of its run.
    """
    rows = db.query(
        "SELECT guid, rating_key FROM programs "
        "WHERE section = 'sports' AND (teams IS NULL OR teams = '[]') "
        "  AND (teams_tried_at IS NULL OR teams_tried_at < ?)",
        (_now() - TEAMS_RETRY_AGE,))
    now = _now()
    asked = 0
    done = 0
    for r in rows:
        if not r["rating_key"]:
            continue
        try:
            m = plex.metadata(provider, r["rating_key"])
        except Exception:
            # A failed call is not an answer, so it is not written down. The
            # row keeps its old note and is asked again next sync.
            continue
        asked += 1
        if not m:
            continue
        teams = [{"id": t.get("id"), "name": t.get("tag")} for t in (m.get("Team") or [])]
        with db.tx() as c:
            if teams:
                c.execute(
                    "UPDATE programs SET teams = ?, teams_tried_at = ?, updated_at = ? "
                    "WHERE guid = ?", (db.js(teams), now, now, r["guid"]))
            else:
                # Asked, and Plex had none. Only the note moves: `updated_at`
                # is what sync_guide prunes on, and this is not a sighting.
                c.execute("UPDATE programs SET teams_tried_at = ? WHERE guid = ?",
                          (now, r["guid"]))
        if teams:
            done += 1
    return asked, done


def sync_teams(plex: Plex, provider: str, sports) -> int:
    """Mirror the teams Plex currently knows, and remember the ones it forgets.

    Plex lists only the teams playing inside the guide window, about eleven
    days. Deleting the rest, which is what this used to do, meant the list you
    pick from shrank to whoever was on this week, and a pass you made in
    September lost its team's name in October.

    So nothing is deleted. A team seen in the guide is marked `in_guide`; one
    that has dropped out keeps its row, its id and its name, and stops being
    marked. The id is the thing that matters: it is what an airing carries and
    what a pass follows.
    """
    if not sports:
        return 0
    now = _now()
    rows = plex.teams(provider, sports)
    with db.tx() as c:
        c.execute("UPDATE teams SET in_guide = 0")
        for t in rows:
            name = t.get("title")
            entry = teamcat.find(name)
            c.execute(
                "INSERT INTO teams (id, name, league, in_guide, last_seen, updated_at) "
                "VALUES (?,?,?,1,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
                "  league=COALESCE(excluded.league, teams.league), "
                "  in_guide=1, last_seen=excluded.last_seen, "
                "  updated_at=excluded.updated_at",
                (int(t.get("key")), name, entry["league"] if entry else None,
                 now, now),
            )
    return len(rows)


def resolve_team_passes() -> int:
    """Point every team pass at the id Plex is using for that team *today*.

    This used to fill in a NULL id and nothing else, on the belief that a team
    id was a stable identity. It is not. Measured on a live server, one guide
    refresh moved the Kansas City Chiefs from 236 to 245 and the Seattle
    Seahawks from 132 to 244, on the same game with the same programme guid. A
    pass holding the old number matched nothing from then on, and said nothing
    about it, because matching nothing is what a team with no games this week
    looks like.

    So the name is the identity and the id is a handle into whatever guide Plex
    currently holds. Every sync re-reads the handle.

    Only teams Plex knows *now* can win it. After a renumber the old row and
    the new one both sit in `teams` under the same name, and picking whichever
    the query happened to return last would have been a coin toss.

    A team that has dropped out of the guide keeps its pass pointed where it
    was, rather than being blanked. Out of season is not the same as renamed,
    and `candidate_airings` matches on the name as well, so the pass keeps
    working either way.

    Returns how many passes were repointed.
    """
    rules = db.query("SELECT id, team_id, team_name FROM passes "
                     "WHERE kind = 'team' AND team_name IS NOT NULL AND team_name != ''")
    if not rules:
        return 0
    known = {}
    for r in db.query("SELECT id, name FROM teams "
                      "WHERE name IS NOT NULL AND in_guide = 1"):
        key = teamcat.norm(r["name"])
        # A name that folds to nothing is not a key. Keying on one collapsed
        # every team written in a non-Latin script into a single entry, and a
        # pass for Zenit came back pointed at the Hanshin Tigers.
        if key:
            known[key] = (r["id"], r["name"])
    done = 0
    with db.tx() as c:
        for p in rules:
            key = teamcat.norm(p["team_name"])
            hit = known.get(key) if key else None
            if hit is None:
                continue
            team_id, plex_name = hit
            if team_id == p["team_id"] and plex_name == p["team_name"]:
                continue
            # Adopt Plex's own spelling as well as its id. A pass made from the
            # shipped catalogue carries the catalogue's spelling, which the
            # guide may never use, and `candidate_airings` compares names
            # strictly on purpose. This is the one place the loose fold is
            # right, and it is where the two spellings are reconciled for good.
            c.execute("UPDATE passes SET team_id = ?, team_name = ? WHERE id = ?",
                      (team_id, plex_name, p["id"]))
            done += 1
    return done


def check_team_passes() -> int:
    """Say so when a team pass can find no game at all.

    The silence is the danger. A pass that matches nothing produces no error
    and no log line, and looks identical to a team that simply is not playing
    this week. That is how a renumber could go unnoticed for a season.

    So a pass with no candidate anywhere in the guide is reported, and the
    notice names the passes rather than saying something is wrong somewhere.
    A team genuinely out of season will trip this too, which is the right
    trade: being told your pass is idle is cheap, and finding out in October
    that it has been idle since August is not.
    """
    idle = []
    for p in db.query("SELECT * FROM passes WHERE kind = 'team' AND enabled = 1"):
        if not passes.candidate_airings(p["team_id"], team_name=p["team_name"]):
            idle.append(p["team_name"] or f"pass {p['id']}")
    now = _now()
    raised = []
    if idle:
        names = ", ".join(sorted(idle))
        raised = [{
            "code": health.TEAM_PASS_UNMATCHED,
            "severity": "warn",
            "title": "A team you follow has no games in the guide",
            "detail": f"Nothing in the guide matches {names}. That is normal out "
                      f"of season. It is not normal in season, and it is what a "
                      f"pass looks like when it has stopped working.",
            "hint": "Check the team is spelled as Plex spells it, and that the "
                    "guide reaches far enough ahead to hold its next game.",
        }]
    health.record(raised, now, owns=health.TEAM_CODES)
    return len(idle)


# A repair cancels and re-books against a live DVR. A sync that decided to do
# that to everything at once would be indistinguishable from a fault, so it is
# capped and the rest waits for the next sync.
MAX_REPAIRS = 10

_BOOKING_SQL = """
    SELECT o.airing_id, o.subscription, o.title, o.program_guid, o.pass_id,
           o.begins_at AS booked_at, o.channel_vcn AS booked_vcn,
           a.begins_at, a.channel_identifier, a.channel_vcn, p.prefs
      FROM our_grabs o
      JOIN passes p ON p.id = o.pass_id
      LEFT JOIN airings a ON a.id = o.airing_id
     WHERE o.source = 'pass'
       AND COALESCE(a.begins_at, o.begins_at) > ?
"""


def _airing_for_schedule(airing_id):
    """One airing, with everything `passes._schedule` needs to re-book it."""
    return db.one(
        """SELECT a.*, p.title, p.grandparent_title, p.rating_key, p.teams,
                  p.summary, p.section
             FROM airings a JOIN programs p ON p.guid = a.program_guid
            WHERE a.id = ?""", (airing_id,))


def _has_grab(key, vcn, begins_at) -> bool:
    """Whether Plex has actually scheduled a recording for this booking.

    Its own question, and the one a settings check would miss. Plex will hold
    a subscription whose settings all read correctly and have nothing
    scheduled against it, which looks healthy from every angle except the one
    that matters.

    Matched on the subscription key or on the broadcast, because the key we
    hold can be the one Plex minted before it re-made the grab.
    """
    return bool(db.one(
        """SELECT 1 FROM plex_grabs
            WHERE (subscription = ? AND ? != '')
               OR (channel_vcn = ? AND begins_at = ?)
            LIMIT 1""", (key or "", key or "", vcn, begins_at)))


def _repair(plex, row, diffs, now):
    """Cancel this booking and make it again from what the pass says now.

    Delete then create, in that order. Creating first would leave two
    subscriptions if the delete then failed, and Plex would record the game
    twice; this way a failure leaves one gap that the next sync fills, which
    is why `verify.can_repair` refuses to run inside two sync intervals of
    kickoff.
    """
    airing = _airing_for_schedule(row["airing_id"])
    if not airing:
        return False, "the airing is no longer in the guide"
    old = row["subscription"]
    if old:
        try:
            plex.delete_subscription(old)
        except PlexError:
            # Already gone is the normal case here: that is often the drift.
            pass
    prefs = dict(db.unjs(row["prefs"]) or {})
    try:
        passes._schedule(plex, airing, None, "pass", prefs=prefs,
                         pass_id=row["pass_id"])
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"
    with db.tx() as c:
        c.execute(
            """INSERT INTO pass_actions (pass_id, program_guid, airing_id,
                                         program_title, channel_vcn, begins_at,
                                         action, reason, dry_run, created_at)
               VALUES (?,?,?,?,?,?,'repaired',?,0,?)""",
            (row["pass_id"], row["program_guid"], row["airing_id"], row["title"],
             airing["channel_vcn"], airing["begins_at"], verify.describe(diffs), now))
    return True, verify.describe(diffs)


def check_bookings(plex, now: int | None = None) -> dict:
    """Is every recording a pass booked still what the pass asks for?

    `passes.already_handled` stops a pass looking at a game twice, which is
    right for booking and wrong for everything after it. Change a pass and its
    existing bookings keep the old settings for ever; Plex drops a
    subscription and nothing notices. Both failures are silent, and both are
    only visible the evening the recording is wrong.

    So every future booking is read back from Plex and compared against the
    pass as it stands now. A real difference is repaired. Anything that cannot
    be repaired safely, because kickoff is close or because the re-book
    failed, becomes a notice instead of a silence.
    """
    now = now or _now()
    lead = verify.repair_lead(int(db.get_setting("sync_minutes") or 60))
    out = {"checked": 0, "repaired": 0, "drifted": 0, "unchecked": 0, "failed": 0}
    drifted, failed = [], []

    for row in db.query(_BOOKING_SQL, (now,)):
        out["checked"] += 1
        begins = row["begins_at"] or row["booked_at"]
        vcn = row["channel_vcn"] or row["booked_vcn"]
        key = row["subscription"]
        if not key:
            # Booked before the key was captured, or Plex was slow to list it.
            # Looking it up beats assuming the recording is gone.
            try:
                key = plex.find_subscription(row["program_guid"], begins)
            except Exception:
                key = None

        if key:
            state, body = plex.subscription_state(key)
        else:
            state, body = "gone", None

        if state == "unknown":
            # Could not ask. Not knowing is never grounds for cancelling.
            out["unchecked"] += 1
            continue

        if state == "gone":
            diffs = verify.no_recording("subscription")
        elif not row["begins_at"]:
            # The guide no longer carries this airing, so there is nothing to
            # compare the pin against and nothing to re-book from.
            out["unchecked"] += 1
            continue
        else:
            have = {s.get("id"): s.get("value") for s in (body.get("Setting") or [])}
            diffs = verify.compare(want=verify.wanted(db.unjs(row["prefs"]), row),
                                   have=have)
            if not _has_grab(key, vcn, begins):
                diffs = diffs + verify.no_recording()

        if not verify.needs_repair(diffs):
            continue

        what = f"{row['title'] or 'a recording'}: {verify.describe(diffs)}"
        if not verify.can_repair(begins_at=begins, now=now, lead=lead):
            out["drifted"] += 1
            drifted.append(what)
            continue
        if out["repaired"] >= MAX_REPAIRS:
            out["drifted"] += 1
            drifted.append(what)
            continue

        ok, detail = _repair(plex, row, diffs, now)
        if ok:
            out["repaired"] += 1
        else:
            out["failed"] += 1
            failed.append(f"{row['title'] or 'a recording'}: {detail}")

    health.record(_booking_notices(drifted, failed), now, owns=health.BOOKING_CODES)
    return out


def _booking_notices(drifted: list[str], failed: list[str]) -> list[dict]:
    """What to say about bookings that could not be put right.

    A repair that worked is not a notice. It is written into the pass history
    and counted in the sync line, which is a permanent record rather than a
    badge that clears itself an hour later. Only what is still wrong belongs
    on the badge.
    """
    raised = []
    if drifted:
        raised.append({
            "code": health.BOOKING_DRIFT,
            "severity": "bad",
            "title": "A scheduled recording does not match its pass",
            "detail": "Plex is holding a recording that no longer matches what "
                      "the pass asks for, and it is too close to the broadcast "
                      "to change safely: " + "; ".join(sorted(drifted)[:5]),
            "hint": "Cancel and re-book it from the Recordings tab if the "
                    "difference matters for this one.",
        })
    if failed:
        raised.append({
            "code": health.BOOKING_REPAIR_FAILED,
            "severity": "bad",
            "title": "A recording could not be put right",
            "detail": "CouchElephant tried to re-book a recording and Plex "
                      "refused: " + "; ".join(sorted(failed)[:5]),
            "hint": "Check the recording in the Recordings tab. The next sync "
                    "will try again.",
        })
    return raised


def network_of(title: str | None) -> str | None:
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


def sync_channels(plex: Plex, dvr_key=None) -> int:
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


KEEP_HISTORY_DAYS = 60


def prune_history(days: int = KEEP_HISTORY_DAYS) -> None:
    """Drop pass bookkeeping for broadcasts long past.

    our_grabs and pass_actions grow by one row per booking and were never
    trimmed. Two months keeps the pass detail's recent history and the
    already-booked check honest without the tables growing forever.
    """
    cutoff = _now() - days * 86400
    with db.tx() as c:
        c.execute("DELETE FROM our_grabs WHERE begins_at < ?", (cutoff,))
        c.execute("DELETE FROM pass_actions WHERE begins_at < ?", (cutoff,))


def sync_recordings(plex: Plex) -> int:
    """Mirror Plex's subscriptions and scheduled grabs.

    Subscriptions include the recurring kind ("All new episodes of X", team
    passes), which is what makes this view worth having.
    """
    now = _now()
    subs = plex.subscriptions()
    # our_grabs holds the subscription key of everything a pass booked.
    ours = {r["subscription"] for r in db.query(
        "SELECT DISTINCT subscription FROM our_grabs WHERE subscription IS NOT NULL")}
    with db.tx() as c:
        for s in subs:
            key = str(s.get("key"))
            # Some server versions carry Setting on the list itself. Only ask
            # for the detail when it does not, rather than one request each.
            detail = s if s.get("Setting") else (plex.subscription(key) or s)
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


def plex_guide_snapshot(plex: Plex) -> tuple[int | None, int | None, list[dict]]:
    """What Plex says about its own guide: when it refreshed, how far it reaches.

    `refreshedAt` is Plex's word for it and is the only place the schedule is
    visible; the furthest airing we hold is the consequence, and the thing that
    actually costs a recording. Both are read every sync so a guide that stops
    moving is visible the next day rather than the week after.
    """
    refreshed = None
    for d in plex.dvrs():
        v = int(d.get("refreshedAt") or 0)
        if v:
            refreshed = max(refreshed or 0, v)
    row = db.one("SELECT MAX(begins_at) m FROM airings")
    ends = int(row["m"]) if row and row["m"] else None
    try:
        tasks = plex.butler_tasks()
    except Exception:
        # An older or stripped-down server may not serve /butler at all. No
        # task list means no schedule to hold Plex to, which the checks read
        # as "cannot say" rather than "not scheduled".
        tasks = []
    return refreshed, ends, tasks


def check_plex_health(plex: Plex) -> int:
    """Raise or clear the notices about Plex's own guide upkeep."""
    refreshed, ends, tasks = plex_guide_snapshot(plex)
    now = _now()
    db.set_setting("epg_refreshed_at", refreshed or "")
    db.set_setting("guide_ends_at", ends or "")
    raised = health.check(tasks=tasks, refreshed_at=refreshed,
                          guide_ends_at=ends, now=now)
    health.record(raised, now, owns=health.PLEX_CODES)
    return len(raised)


def full_sync() -> tuple[int, str]:
    """One pass over everything. Returns a short human-readable summary."""
    started = _now()
    detail = ""
    ok = 0
    try:
        with Plex(db.get_setting("plex_url"), db.get_setting("plex_token")) as plex:
            ok, detail = _sync_everything(plex)
    except Exception as e:
        # Keep the frame that actually failed. A bare type+message sent me
        # chasing the wrong module once already.
        tb = traceback.extract_tb(e.__traceback__)
        where = " <- ".join(f"{f.name}:{f.lineno}" for f in reversed(tb[-4:]))
        detail = f"{type(e).__name__}: {e} [{where}]"
    if not ok:
        # A sync that never reached Plex has no snapshot to check, so the
        # failure itself is the notice. Raised here rather than in
        # `_sync_everything`, which does not run when the connection is what
        # broke.
        health.record(health.unreachable(detail), _now(), owns=health.REACH_CODES)
    with db.tx() as c:
        c.execute("INSERT INTO sync_log (started_at, ended_at, ok, detail, "
                  "                      epg_refreshed_at, guide_ends_at) "
                  "VALUES (?,?,?,?,?,?)",
                  (started, _now(), ok, detail,
                   _int_or_none(db.get_setting("epg_refreshed_at")),
                   _int_or_none(db.get_setting("guide_ends_at"))))
        c.execute("DELETE FROM sync_log WHERE id NOT IN "
                  "(SELECT id FROM sync_log ORDER BY id DESC LIMIT 50)")
    return ok, detail


def _sync_everything(plex):
    """Every sync step in order. Raises on failure; full_sync logs it."""
    provider, shows, sports, movies = discover(plex)
    db.set_setting("epg_provider", provider)
    db.set_setting("shows_section", shows or "")
    db.set_setting("sports_section", sports or "")
    db.set_setting("movies_section", movies or "")

    sync_channels(plex)
    teams = sync_teams(plex, provider, sports)
    # A pass made for a team that had not played yet starts working here,
    # the moment the guide first carries it.
    woke = resolve_team_passes()
    guide = sync_guide(plex, provider, shows, sports, movies)
    asked, enriched = enrich_sports(plex, provider)
    got, bad, tried = cache_logos()
    subs = sync_recordings(plex)
    prune_history()
    check_plex_health(plex)
    idle = check_team_passes()
    # The guide may have just reached something a pass has been waiting months
    # for. Bind it before the booking check runs, so it records on this sync
    # rather than the next one.
    promoted = expectations.promote(now=_now())
    # Judged against how far the guide actually reaches, not against today.
    # Before the guide gets there, silence means a short guide and not a
    # missing show.
    _now_at = _now()
    health.record(
        expectations.sweep_misses(
            _int_or_none(db.get_setting("guide_ends_at")), _now_at),
        _now_at, owns=health.EXPECT_CODES)
    # After sync_recordings, so the grab check reads Plex's current schedule
    # rather than last hour's.
    book = check_bookings(plex)

    cov = logo_coverage()
    nch = cov["channels"]
    detail = (f"{guide['programs']} programs, {guide['airings']} airings, "
              f"{teams} teams, {enriched} sports enriched"
              + (f" ({asked} asked)" if asked else "") + ", "
              + (f"{woke} pass(es) repointed, " if woke else "")
              + (f"{promoted} now in the guide, " if promoted else "")
              + (f"{idle} team pass(es) matching nothing, " if idle else "")
              + (f"{book['repaired']} recording(s) repaired, " if book["repaired"] else "")
              + (f"{book['drifted']} recording(s) adrift, " if book["drifted"] else "")
              + f"{nch} channels, logos {cov['with_logo']}/{cov['channels']}"
              + (f" (+{got} fetched)" if got else "")
              + (f" ({bad} failed)" if bad else "")
              + f", {subs} subscriptions")
    return 1, detail
