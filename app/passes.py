"""Smart Sports Pass.

Plex has a team pass already. It is broken for the case it exists for: when a
game airs live and is then repeated, Plex can schedule the repeat. Verified on a
live server, with the guide plainly flagging the live broadcast `premiere: 1`
and Plex choosing the unflagged rebroadcast anyway.

This does the one thing Plex gets wrong. Rather than register a recurring
subscription and hope, it reads the guide itself, picks the airing deliberately,
and creates a one-shot recording pinned to that exact channel and start time so
Plex has nothing left to choose between.

Selection order for a game:
  1. Drop DRM airings. Nothing can decrypt them.
  2. Prefer an airing flagged `premiere` (the live broadcast).
  3. Among equals, take the earliest start.
"""
import time

from . import db
from .plex import Plex, PlexError

# Give a game this much slack before treating it as too late to schedule.
LEAD_SECONDS = 120


def _now():
    return int(time.time())


def candidate_airings(team_id, horizon_days=30):
    """Future airings of games featuring this team, newest guide data only."""
    cutoff = _now() - LEAD_SECONDS
    limit = _now() + horizon_days * 86400
    rows = db.query(
        """SELECT a.*, p.title, p.grandparent_title, p.rating_key, p.teams, p.summary
           FROM airings a JOIN programs p ON p.guid = a.program_guid
           WHERE a.begins_at BETWEEN ? AND ?
           ORDER BY a.begins_at""",
        (cutoff, limit),
    )
    out = []
    for r in rows:
        teams = db.unjs(r["teams"])
        if any(int(t.get("id") or -1) == int(team_id) for t in teams):
            out.append(r)
    return out


def choose_airing(airings):
    """Pick one broadcast of a game, and say why. Returns (row, reason)."""
    usable = [a for a in airings if not a["drm"]]
    if not usable:
        return None, "every airing is DRM encrypted and cannot be recorded"
    premieres = [a for a in usable if a["premiere"]]
    if premieres:
        pick = min(premieres, key=lambda a: a["begins_at"])
        if len(usable) > 1:
            return pick, f"live broadcast (premiere) of {len(usable)} airings"
        return pick, "only airing, flagged premiere"
    pick = min(usable, key=lambda a: a["begins_at"])
    return pick, ("no airing is flagged premiere; took the earliest of "
                  f"{len(usable)}")


def group_by_game(rows):
    games = {}
    for r in rows:
        games.setdefault(r["program_guid"], []).append(r)
    return games


def already_handled(program_guid, airing_id):
    """True if we, or Plex, already have this game covered."""
    mine = db.one(
        "SELECT 1 FROM pass_actions WHERE program_guid = ? AND action = 'scheduled' "
        "AND dry_run = 0 LIMIT 1", (program_guid,))
    if mine:
        return "already scheduled by a pass"
    prog = db.one("SELECT title FROM programs WHERE guid = ?", (program_guid,))
    if prog:
        hit = db.one(
            "SELECT status FROM plex_grabs WHERE title = ? AND status IN "
            "('scheduled','inprogress','complete') LIMIT 1", (prog["title"],))
        if hit:
            return f"Plex already has it ({hit['status']})"
    return None


def _log(pass_id, row, action, reason, dry_run, subscription=None):
    with db.tx() as c:
        c.execute(
            """INSERT INTO pass_actions (pass_id, program_guid, airing_id, program_title,
                                         channel_vcn, begins_at, action, reason,
                                         plex_subscription, dry_run, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (pass_id, row["program_guid"] if row else None,
             row["id"] if row else None,
             row["title"] if row else None,
             row["channel_vcn"] if row else None,
             row["begins_at"] if row else None,
             action, reason, subscription, 1 if dry_run else 0, _now()),
        )


def _schedule(plex, row, target_section):
    """Create a one-shot recording pinned to exactly this broadcast."""
    options = plex.template(row["rating_key"])
    single = None
    for t in options:
        for s in (t.get("MediaSubscription") or []):
            title = (s.get("title") or "").lower()
            if title.startswith("this "):
                single = s
                break
        if single:
            break
    if not single:
        raise PlexError("Plex offered no single-event recording option")

    prefs = {
        "oneShot": "1",
        # These two are what stop Plex picking a different airing later.
        "lineupChannel": row["channel_identifier"] or "",
        "startTimeslot": str(row["begins_at"]),
    }
    plex.create_recording(
        single["parameters"],
        target_section or single.get("targetLibrarySectionID") or 2,
        int(single.get("type") or 4),
        prefs,
    )
    return single.get("targetLibrarySectionID")


def run_passes(force_dry_run=None):
    """Evaluate every enabled pass. Returns a list of decision dicts."""
    dry = db.get_setting("dry_run") == "1" if force_dry_run is None else force_dry_run
    results = []
    rows = db.query("SELECT * FROM passes WHERE enabled = 1")
    if not rows:
        return results

    plex = None
    if not dry:
        plex = Plex(db.get_setting("plex_url"), db.get_setting("plex_token"))

    for p in rows:
        games = group_by_game(candidate_airings(p["team_id"]))
        for guid, airings in games.items():
            pick, reason = choose_airing(airings)
            if not pick:
                _log(p["id"], airings[0], "skipped", reason, dry)
                results.append({"pass": p["team_name"], "game": airings[0]["title"],
                                "action": "skipped", "reason": reason})
                continue
            blocked = already_handled(guid, pick["id"])
            if blocked:
                results.append({"pass": p["team_name"], "game": pick["title"],
                                "action": "skipped", "reason": blocked,
                                "channel": pick["channel_vcn"], "begins_at": pick["begins_at"]})
                continue
            if dry:
                _log(p["id"], pick, "would schedule", reason, True)
                results.append({"pass": p["team_name"], "game": pick["title"],
                                "action": "would schedule", "reason": reason,
                                "channel": pick["channel_vcn"], "begins_at": pick["begins_at"]})
                continue
            try:
                _schedule(plex, pick, None)
                _log(p["id"], pick, "scheduled", reason, False)
                results.append({"pass": p["team_name"], "game": pick["title"],
                                "action": "scheduled", "reason": reason,
                                "channel": pick["channel_vcn"], "begins_at": pick["begins_at"]})
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                _log(p["id"], pick, "failed", msg, False)
                results.append({"pass": p["team_name"], "game": pick["title"],
                                "action": "failed", "reason": msg,
                                "channel": pick["channel_vcn"], "begins_at": pick["begins_at"]})
    return results
