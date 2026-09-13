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

from . import db, smartfilter, teamcat
from .plex import Plex, PlexError

# Give a game this much slack before treating it as too late to schedule.
LEAD_SECONDS = 120


def _now():
    return int(time.time())


_AIRING_SQL = """SELECT a.*, p.title, p.grandparent_title, p.rating_key, p.teams, p.summary,
                  p.section, c.network AS channel_network
           FROM airings a
           JOIN programs p ON p.guid = a.program_guid
           LEFT JOIN channels c ON c.vcn = a.channel_vcn
           WHERE a.begins_at BETWEEN ? AND ?"""


def _future(extra="", args=(), horizon_days=30, limit=None, program_guid=None):
    """Future airings with a WHERE fragment applied by SQLite, not by Python.

    The guide holds around twenty thousand future airings. Pulling them all in
    to keep a dozen is how a pass list of forty took a second to draw.
    """
    cutoff = _now() - LEAD_SECONDS
    until = _now() + horizon_days * 86400
    # Narrowing to one programme is done here rather than by the caller, so
    # `idx_airings_program` serves it instead of the whole window being
    # hydrated and then thrown away.
    if program_guid:
        extra = f"a.program_guid = ?{f' AND {extra}' if extra else ''}"
        args = (program_guid, *args)
    sql = _AIRING_SQL + (f" AND {extra}" if extra else "") + " ORDER BY a.begins_at"
    if limit:
        sql += f" LIMIT {int(limit)}"
    return db.query(sql, (cutoff, until, *args))


def candidate_airings(team_id: int | None, horizon_days: int = 30,
                      team_name: str | None = None, program_guid: str | None = None,
                      limit: int | None = None) -> list:
    """Future airings of games featuring this team.

    Matched on the team's NAME. The id is a fallback and nothing more.

    A pass holds an id from the `teams` table, which is Plex's section-level
    team list. A programme holds its own `teams` array, numbered per programme.
    THEY ARE DIFFERENT NUMBERINGS, and comparing one to the other is a category
    error rather than a stale lookup.

    Measured on the live guide on 2026-09-10: of the 89 ids that appear in both,
    71 name a different team in each. The section list calls 253 "Nevada" while
    a programme calls it "Sporting Kansas City". Inside the programme arrays
    alone, id 343 was Kansas City Chiefs, Borussia Dortmund, Los Angeles Rams
    and San Diego State.

    This used to match on the id OR the name, and the id half is what turned a
    Kansas City Chiefs pass into one that also tried to record Bundesliga
    football, two college games and a Bears game: 142 refused bookings. The old
    reasoning was that a pass and a programme might sit either side of a
    renumbering, so both halves were needed. That is only sound if the two ids
    name the same thing, and they never did.

    The id moves as well: one refresh took the Chiefs from 236 to 245 on the
    same game with the same programme guid. The name is what survives that,
    which is why it is the only thing consulted when a pass has one.

    The name is compared through `teamcat.ident`, which folds case, accents and
    punctuation and nothing else. Deliberately not `teamcat.norm`: that also
    drops club words, which is right for *finding* a team in the catalogue and
    wrong for deciding what to record. It folds "Real Madrid" and "Atletico
    Madrid" both to "madrid", and does the same to Cincinnati and FC
    Cincinnati, and to four more pairs in the shipped catalogue.

    Spelling is kept in step at the other end: `sync.resolve_team_passes` adopts
    Plex's own spelling for the team when it repoints a pass, so a pass made
    from the catalogue stops carrying a name the guide has never used.
    """
    if not team_id and not team_name:
        return []
    key = teamcat.ident(team_name or "")
    if key:
        return _future("EXISTS (SELECT 1 FROM json_each(p.teams) t "
                       "WHERE tident(json_extract(t.value, '$.name')) = ?)",
                       (key,), horizon_days, limit=limit, program_guid=program_guid)
    # No name to go on. An old pass made before the name was stored has only
    # the id, and a wrong match is still better than a pass that matches
    # nothing at all and looks like a quiet week.
    return _future("EXISTS (SELECT 1 FROM json_each(p.teams) t "
                   "WHERE json_extract(t.value, '$.id') = ?)",
                   (int(team_id or 0),), horizon_days, limit=limit,
                   program_guid=program_guid)


def series_airings(series_guid: str, horizon_days: int = 30,
                   program_guid: str | None = None) -> list:
    """Future airings of one programme, wherever it turns up.

    Matched on the show rather than the episode, so a rule follows the series
    across every episode the guide holds.
    """
    if not series_guid:
        return []
    return _future("(a.program_guid = ? OR p.grandparent_title = ?)",
                   (series_guid, series_guid), horizon_days,
                   program_guid=program_guid)


def smart_airings(tree: dict, horizon_days: int = 30,
                  program_guid: str | None = None) -> list:
    """Future airings matching a smart filter.

    Compiled to SQL and asked of the database, rather than pulled into Python
    and sifted. A loose filter can match thousands of rows, and the whole point
    of the count in the panel is that it comes back before the user has given up.
    """
    frag, args = smartfilter.build(tree)
    return _future(f"({frag})", args, horizon_days, program_guid=program_guid)


def any_airing(horizon_days: int = 30) -> list:
    """One usable broadcast, whichever. A stand-in when nothing is chosen yet.

    Plex's recording settings belong to Plex, not to the programme: every
    template offers the same ones. So a pass can be shown them before it knows
    what it follows, which is the difference between an options panel and an
    empty box that says come back later.
    """
    return _future("COALESCE(a.drm, 0) = 0", (), horizon_days, limit=1)


def rule_airings(rule, horizon_days: int = 30, program_guid: str | None = None) -> list:
    """Everything a rule could record, before the source limit is applied.

    `program_guid` narrows the answer to one programme, in SQL. A caller that
    wants one game was otherwise served the pass's whole thirty-day window and
    threw all but one programme away, which is a full scan of the guide with a
    `json_each` per row for a result an index can find.
    """
    if rule["kind"] == "smart":
        return smart_airings(db.unjs(rule["filter"], {}) or {}, horizon_days,
                             program_guid=program_guid)
    if rule["kind"] == "series":
        return series_airings(rule["series_guid"] or rule["series_title"],
                              horizon_days, program_guid=program_guid)
    # The name goes with the id. A pass followed from the catalogue has only a
    # name until the team first plays, and after Plex renumbers, the name is
    # the half that is still true.
    return candidate_airings(rule["team_id"], horizon_days,
                             team_name=rule["team_name"], program_guid=program_guid)


def allowed_sources(rule) -> tuple[list[str], list[str]]:
    """The networks and channels a rule accepts. Empty means anywhere."""
    return (db.unjs(rule["networks"]) or [], db.unjs(rule["channels"]) or [])


def in_sources(row, networks: list[str], channels: list[str]) -> bool:
    """True when this broadcast comes from somewhere the rule accepts.

    The two lists are one allowlist, not two filters that both have to pass.
    Naming a network and a channel means "either of these", which is what a
    person means by "only ABC, CBS and channel 41.1".
    """
    if not networks and not channels:
        return True
    if row["channel_vcn"] in channels:
        return True
    try:
        net = row["channel_network"]
    except (IndexError, KeyError):
        # A row shape without the column. Cheaper to ask forgiveness than to
        # build and scan a list of column names for every airing.
        net = None
    return bool(net and net in networks)


def choose_airing(airings: list) -> tuple[dict | None, str]:
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


def group_by_game(rows: list) -> dict[str, list]:
    games = {}
    for r in rows:
        games.setdefault(r["program_guid"], []).append(r)
    return games


def last_read() -> int:
    """When Plex was last read back into our copy.

    A constant for the length of one pass run, so `run_passes` reads it once
    and hands it down rather than paying for it per game.
    """
    return db.one("SELECT COALESCE(MAX(started_at), 0) t FROM sync_log "
                  "WHERE ok = 1")["t"]


def already_handled(program_guid: str, read_at: int | None = None) -> str | None:
    """Why this game needs no booking, or None.

    THE LIVE STATE, NEVER THE LOG. This used to answer yes to any programme
    that had ever been written down as scheduled, which is a different
    question and the wrong one. A booking can be lost after it is made: Plex
    drops a subscription on its own, the user cancels one, or the guide
    re-times the broadcast and the pinned recording stops existing. In every
    one of those the log still said scheduled, so the pass never looked at the
    game again and nothing recorded it. A stale record read as consent, which
    is the same shape as a failed lookup read as permission.

    Matching Plex's grabs by title alone treated any programme that keeps its
    title, a daily news or quiz show, as already covered for ever. A programme
    guid names one episode and so one game, which is why it is what is asked
    about here: the rebroadcast of a game shares its programme, and recording
    either of them is recording the game.
    """
    # Our own booking, and then whether it is real. `our_grabs` alone is an
    # intention; Plex listing the subscription is the fact. The second half of
    # the OR is the moment between booking one and reading Plex back: the row
    # exists, `plex_subscriptions` is only refreshed by a sync and cannot see
    # it yet, and without this two runs in a row would book the game twice.
    if read_at is None:
        read_at = last_read()
    mine = db.one(
        """SELECT 1 FROM our_grabs o
             LEFT JOIN plex_subscriptions s ON s.key = o.subscription
            WHERE o.program_guid = ?
              AND (s.key IS NOT NULL OR o.created_at >= ?) LIMIT 1""",
        (program_guid, read_at))
    if mine:
        return "already booked by a pass"
    hit = db.one(
        """SELECT g.status FROM plex_grabs g
           JOIN airings a ON a.channel_vcn = g.channel_vcn AND a.begins_at = g.begins_at
           WHERE a.program_guid = ?
             AND g.status IN ('scheduled','inprogress','complete')
           LIMIT 1""", (program_guid,))
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


def templates(plex: Plex, row) -> list[dict]:
    """Every recording option Plex offers for this programme, in its order."""
    out = []
    for t in plex.template(row["rating_key"]):
        out.extend(t.get("MediaSubscription") or [])
    return out


# Plex's own subscription types. 4 is one broadcast; 2 is a series or a
# league; 15 is a team. Anything that is not 4 recurs.
ONE_SHOT_TYPE = 4


def is_one_shot(template: dict) -> bool:
    """Whether this template books one broadcast rather than a rule.

    Read from Plex's `type`, not from its title. The title is localized: a
    German server offers "Diese Sendung", not "This Episode", and matching the
    English word made the whole app pick the wrong template for anyone whose
    Plex is not in English. The title is only a fallback for a server old
    enough not to send a type at all.
    """
    kind = template.get("type")
    if kind not in (None, ""):
        try:
            return int(kind) == ONE_SHOT_TYPE
        except (TypeError, ValueError):
            pass
    return (template.get("title") or "").lower().startswith("this ")


def single_template(options: list[dict]) -> dict | None:
    """The one-shot option: the one that books this broadcast and no other."""
    for s in options:
        if is_one_shot(s):
            return s
    return None


def offered_prefs(template: dict, prefs: dict) -> dict:
    """Only the settings this template actually offers.

    Plex refuses a whole booking if one setting is not on the template's own
    list, and it refuses it with a bare HTML 400 page, in no measurable time,
    with nothing in its log. Read against the client that is a network fault,
    not a bad request, which is exactly how it went unnoticed.

    That is not hypothetical. A team pass made before the settings panel
    started hiding them kept `onlyNewAirings` in its stored settings. A pass
    always books the one-shot template, and the one-shot template does not
    offer that setting, so every booking failed from 2026-08-22 to 2026-09-10:
    364 attempts, two per hour, and no Chiefs game recorded.

    A template that declares no settings is an unknown server, not a server
    that allows none, so nothing is dropped in that case.
    """
    ids = {s.get("id") for s in (template.get("Setting") or []) if s.get("id")}
    if not ids:
        return dict(prefs)
    return {k: v for k, v in prefs.items() if k in ids}


def _schedule(plex, row, target_section, source="pass", template=None, prefs=None,
              pass_id=None):
    """Create a recording for this broadcast.

    With no template given this is the pinned one-shot the passes rely on.
    The overlay passes its own template and settings, so the user gets the
    same choices Plex itself offers.
    """
    chosen = template
    if chosen is None:
        chosen = single_template(templates(plex, row))
        if not chosen:
            raise PlexError("Plex offered no single-event recording option")

    # Drop anything this template will not take, before the pins go on. Plex
    # answers 400 to the whole request otherwise, and says nothing about why.
    prefs = offered_prefs(chosen, dict(prefs or {}))
    # These three are not the user's to change on a pass booking. The pin is
    # the whole mechanism: without it Plex picks the airing itself, which is
    # the bug this app exists to fix. They go on AFTER the filter, so a server
    # that declares no settings at all still gets a pinned booking.
    prefs.update({
        "oneShot": "1",
        "lineupChannel": row["channel_identifier"] or "",
        "startTimeslot": str(row["begins_at"]),
    })
    key = plex.create_recording(
        chosen["parameters"],
        target_section or chosen.get("targetLibrarySectionID") or 2,
        int(chosen.get("type") or 4),
        prefs,
    )
    # Plex answers 200 and hands back a key, then sometimes drops the
    # subscription by itself. Reporting a recording it did not keep is worse
    # than failing, so check before claiming anything.
    # `is False` on purpose. None means the check could not be made, and a
    # network blip must not be reported as "Plex discarded your recording".
    if key and plex.subscription_exists(key) is False:
        raise PlexError(
            "Plex accepted the recording and then discarded it. That usually "
            "means it already has this episode, or the airing is a repeat and "
            "the rule is set to new airings only.")
    if not key:
        # Older path: no key in the reply, so look it up.
        try:
            key = plex.find_subscription(row["program_guid"], row["begins_at"], tries=3)
        except Exception:
            key = None
    remember(row, source, key, pass_id)
    return chosen.get("targetLibrarySectionID")


def remember(row, source: str, subscription: str | None = None,
             pass_id: int | None = None) -> None:
    """Record that this airing was scheduled by us, and by what.

    The pass is recorded by uid as well as by id, because `id` is an
    autoincrement and means nothing on another machine.
    """
    with db.tx() as c:
        c.execute(
            """INSERT INTO our_grabs (airing_id, program_guid, title, channel_vcn,
                                      begins_at, source, subscription, pass_id,
                                      pass_uid, created_at)
               VALUES (?,?,?,?,?,?,?,?,
                       (SELECT uid FROM passes WHERE id = ?),?)
               ON CONFLICT(airing_id) DO UPDATE SET source=excluded.source,
                 subscription=COALESCE(excluded.subscription, our_grabs.subscription),
                 pass_id=COALESCE(excluded.pass_id, our_grabs.pass_id),
                 pass_uid=COALESCE(excluded.pass_uid, our_grabs.pass_uid),
                 created_at=excluded.created_at""",
            (row["id"], row["program_guid"], row["title"], row["channel_vcn"],
             row["begins_at"], source, subscription, pass_id, pass_id, _now()))


def forget(airing_id) -> None:
    """Drop our record of an airing, after the recording has been cancelled."""
    with db.tx() as c:
        c.execute("DELETE FROM our_grabs WHERE airing_id = ?", (airing_id,))


def rule_label(rule) -> str:
    keys = rule.keys()
    if rule["kind"] == "smart":
        named = rule["label"] if "label" in keys else None
        if named:
            return named
        return smartfilter.describe(db.unjs(rule["filter"], {}) or {})
    return rule["team_name"] or rule["series_title"] or "rule"


def run_passes(force_dry_run: bool | None = None) -> list[dict]:
    """Evaluate every enabled pass. Returns a list of decision dicts."""
    dry = db.get_setting("dry_run") == "1" if force_dry_run is None else force_dry_run
    results = []
    rows = db.query("SELECT * FROM passes WHERE enabled = 1")
    if not rows:
        return results

    plex = None
    if not dry:
        plex = Plex(db.get_setting("plex_url"), db.get_setting("plex_token"))
    try:
        _evaluate(rows, plex, dry, results)
    finally:
        if plex is not None:
            plex.close()
    return results


def _evaluate(rows, plex, dry, results):
    # Read once for the whole run. It cannot change while this is running, and
    # it was being asked once per game per pass.
    read_at = last_read()
    for p in rows:
        label = rule_label(p)
        networks, channels = allowed_sources(p)
        try:
            games = group_by_game(rule_airings(p))
        except smartfilter.FilterError as e:
            # One unusable filter must not stop every other pass running.
            _log(p["id"], None, "failed", f"the filter cannot be read: {e}", dry)
            results.append({"pass": label, "game": "", "action": "failed",
                            "reason": f"the filter cannot be read: {e}"})
            continue
        for guid, airings in games.items():
            # The source limit is applied before the choice, not after, so the
            # rule picks the best airing among the ones it is allowed to use
            # rather than picking first and then finding it disallowed.
            allowed = [a for a in airings if in_sources(a, networks, channels)]
            if not allowed:
                where = " or ".join(networks + channels)
                reason = f"no airing is on {where}"
                _log(p["id"], airings[0], "skipped", reason, dry)
                results.append({"pass": label, "game": airings[0]["title"],
                                "action": "skipped", "reason": reason})
                continue
            pick, reason = choose_airing(allowed)
            if networks or channels:
                reason += f", limited to {' or '.join(networks + channels)}"
            if not pick:
                _log(p["id"], allowed[0], "skipped", reason, dry)
                results.append({"pass": label, "game": allowed[0]["title"],
                                "action": "skipped", "reason": reason})
                continue
            blocked = already_handled(guid, read_at)
            if blocked:
                results.append({"pass": label, "game": pick["title"],
                                "action": "skipped", "reason": blocked,
                                "channel": pick["channel_vcn"], "begins_at": pick["begins_at"]})
                continue
            if dry:
                _log(p["id"], pick, "would schedule", reason, True)
                results.append({"pass": label, "game": pick["title"],
                                "action": "would schedule", "reason": reason,
                                "channel": pick["channel_vcn"], "begins_at": pick["begins_at"]})
                continue
            try:
                _schedule(plex, pick, None, "pass", prefs=dict(db.unjs(p["prefs"]) or {}),
                          pass_id=p["id"])
                _log(p["id"], pick, "scheduled", reason, False)
                results.append({"pass": label, "game": pick["title"],
                                "action": "scheduled", "reason": reason,
                                "channel": pick["channel_vcn"], "begins_at": pick["begins_at"]})
            except Exception as e:
                msg = f"{type(e).__name__}: {e}"
                _log(p["id"], pick, "failed", msg, False)
                results.append({"pass": label, "game": pick["title"],
                                "action": "failed", "reason": msg,
                                "channel": pick["channel_vcn"], "begins_at": pick["begins_at"]})
