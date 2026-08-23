"""Recording one broadcast, and the pieces a pass shares with it:
Plex's templates, their settings, and making a pass."""
import asyncio
import time
import urllib.parse
import uuid

from fastapi import APIRouter, Form
from fastapi.responses import JSONResponse

from .. import db, passes, smartfilter, sync
from ..plex import PlexError
from ._shared import _plex

router = APIRouter()


# Settings Plex exposes but does not label. It hides them in its own UI, and
# they are plumbing rather than choices: oneShot is what makes a recording a
# single event at all.
_HIDDEN_SETTINGS = {"oneShot", "remoteMedia", "comskipEnabled"}


def _enum(raw):
    """Plex packs a setting's choices as `value:Label|value:Label`.

    Labels are URL encoded inside that string, so `07%3A00 PM` has to be
    decoded or every time option reads as gibberish.
    """
    out = []
    for part in (raw or "").split("|"):
        if not part:
            continue
        value, _, label = part.partition(":")
        out.append({"value": value, "label": urllib.parse.unquote(label) or "Any"})
    return out


# The two padding fields go last, and in that order. Plex lists them in the
# middle, between "allow partial airings" and commercial detection, which
# splits the pair that people actually reach for. Everything else keeps Plex's
# own order.
_LAST_SETTINGS = ("startOffsetMinutes", "endOffsetMinutes")


def _ordered(settings):
    rest = [s for s in settings if s["id"] not in _LAST_SETTINGS]
    tail = [s for k in _LAST_SETTINGS for s in settings if s["id"] == k]
    return rest + tail


# A pass always pins each booking to the airing it chose, so these three are
# not its to carry. Storing them is noise at best, and a reader could believe
# a pass had been limited to one channel when it had not.
_PASS_PREF_BLOCKED = frozenset(("oneShot", "lineupChannel", "startTimeslot"))

# What a CouchElephant pass books, every time, is a pinned one-shot. So the
# settings it should be offered are the one-shot template's. The recurring
# template carries three more that mean nothing to a one-shot booking: whether
# to take repeats, and two policies about deleting episodes it has kept.
_RECURRING_ONLY = frozenset(("onlyNewAirings",
                             "autoDeletionItemPolicyUnwatchedLibrary",
                             "autoDeletionItemPolicyWatchedLibrary"))

# CouchElephant sets these itself, per airing. Offering them on a pass would be
# a control that does nothing: `_pass_prefs` drops them on the way in, because
# the pin is the mechanism the whole app exists for.
_PASS_HIDDEN = _RECURRING_ONLY | _PASS_PREF_BLOCKED

# Sport overruns. It is the normal case, not the exception, and a pass with no
# padding clips the end of every game. Offered as a filled-in default on a new
# sports pass, on screen, before anything is created.
SPORTS_PADDING = {"startOffsetMinutes": "1", "endOffsetMinutes": "30"}

# Plex sends the padding fields as a plain integer with no list of allowed
# values, so any number works and there is no ceiling to respect. These are
# suggestions, not limits: the field still takes anything typed into it. A
# game running two hours long is why 120 is on the list.
_PRESETS = {
    "startOffsetMinutes": [0, 1, 2, 5, 10, 15, 30],
    "endOffsetMinutes": [0, 5, 15, 30, 45, 60, 90, 120, 180],
}


def _template_payload(options, row=None, pin=True):
    """Plex's own recording choices, ready to render.

    Settings Plex leaves unlabelled are plumbing and stay hidden, the same as
    in its own dialog. For a single broadcast the channel and the airing time
    arrive already pinned, which is the whole point of this app.
    """
    out = []
    for i, s_ in enumerate(options):
        title = s_.get("title") or "Record"
        one_shot = title.lower().startswith("this ")
        settings = []
        for st in (s_.get("Setting") or []):
            sid = st.get("id")
            if sid in _HIDDEN_SETTINGS or not (st.get("label") or "").strip():
                continue
            value = str(st.get("value"))
            if pin and one_shot and row is not None and sid == "lineupChannel":
                value = row["channel_identifier"] or value
            if pin and one_shot and row is not None and sid == "startTimeslot":
                value = str(row["begins_at"])
            settings.append({
                "id": sid, "label": st.get("label"), "type": st.get("type"),
                # Plex explains its own settings. Repeating those words here
                # would be a second copy to keep true.
                "hint": (st.get("summary") or "").strip(),
                "value": value, "options": _enum(st.get("enumValues")),
                "presets": _PRESETS.get(sid, []),
            })
        out.append({
            "index": i, "title": title, "type": s_.get("type"),
            "one_shot": one_shot, "settings": _ordered(settings),
        })
    return out


def _airing_row(airing_id):
    return db.one(
        """SELECT a.*, p.rating_key, p.title, p.grandparent_title, p.teams
           FROM airings a JOIN programs p ON p.guid = a.program_guid
           WHERE a.id = ?""", (airing_id,))


@router.get("/api/record/options")
async def api_record_options(airing_id: str):
    """What Plex offers for this programme, read from Plex rather than guessed.

    Reading the template means any option Plex adds later appears here without
    a change, and the labels are its own.
    """
    row = _airing_row(airing_id)
    if not row:
        return JSONResponse({"error": "airing not found"}, status_code=404)
    if row["drm"]:
        return JSONResponse({"error": "This airing is DRM encrypted and cannot be recorded."},
                            status_code=400)
    try:
        with _plex() as plex:
            options = await asyncio.to_thread(passes.templates, plex, row)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=502)

    out = _template_payload(options, row)
    # Where a recording could come from. Networks first, because "only ABC,
    # CBS and FOX" is how people say it; channels are the finer grain.
    nets, chans = {}, []
    for c in db.query("SELECT vcn, call_sign, network FROM channels "
                      "ORDER BY CAST(vcn AS REAL)"):
        if c["network"]:
            nets.setdefault(c["network"], []).append(c["vcn"])
        chans.append({"vcn": c["vcn"], "call_sign": c["call_sign"] or "",
                      "network": c["network"] or ""})
    return JSONResponse({
        "ok": True, "title": row["title"], "templates": out,
        "dry_run": db.get_setting("dry_run") == "1",
        "networks": [{"name": n, "channels": v} for n, v in
                     sorted(nets.items(), key=lambda kv: kv[0].lower())],
        "channels": chans,
    })




def _pass_prefs(prefs):
    return {k: v for k, v in (prefs or {}).items() if k not in _PASS_PREF_BLOCKED}


def _make_pass(kind, team=None, series=None, nets=None, chans=None, prefs=None,
               update=True, smart=None, name=""):
    """Create or update one CouchElephant pass.

    The only place a pass is written. There were three, and they had already
    drifted: one checked for a duplicate, one checked the wrong column, one did
    not check at all.

    Returns (pass_id, label, created).
    """
    nets, chans, prefs = nets or [], chans or [], _pass_prefs(prefs)
    if kind == "smart":
        # Two smart passes with the same conditions are the same pass. Compared
        # on the stored JSON, so a reordered tree counts as a different one,
        # which is the honest answer: it may well match different things.
        existing = db.one("SELECT id FROM passes WHERE kind='smart' AND filter = ?",
                          (db.js(smart),))
        label = (name or "").strip() or smartfilter.describe(smart)
    elif kind == "team":
        # A team picked from the catalogue has no Plex id until it plays, so
        # the duplicate check falls back to the name. Otherwise every unplayed
        # team looks like the same pass, because they all have id NULL.
        if team.get("id"):
            existing = db.one("SELECT id FROM passes WHERE kind='team' AND team_id = ?",
                              (team["id"],))
        else:
            existing = db.one(
                "SELECT id FROM passes WHERE kind='team' AND team_id IS NULL "
                "AND team_name = ?", (team["name"],))
        label = team["name"]
    else:
        existing = db.one("SELECT id FROM passes WHERE kind='series' AND series_title = ?",
                          (series,))
        label = series

    if existing and not update:
        return existing["id"], label, False

    with db.tx() as c:
        if existing:
            c.execute("UPDATE passes SET networks=?, channels=?, prefs=?, enabled=1 "
                      "WHERE id=?",
                      (db.js(nets), db.js(chans), db.js(prefs), existing["id"]))
            return existing["id"], label, False
        uid = uuid.uuid4().hex
        if kind == "smart":
            c.execute("INSERT INTO passes (kind, filter, label, networks, channels, "
                      "prefs, uid, enabled, created_at) VALUES ('smart',?,?,?,?,?,?,1,?)",
                      (db.js(smart), label, db.js(nets), db.js(chans),
                       db.js(prefs), uid, int(time.time())))
        elif kind == "team":
            c.execute("INSERT INTO passes (kind, team_id, team_name, networks, channels, "
                      "prefs, uid, enabled, created_at) VALUES ('team',?,?,?,?,?,?,1,?)",
                      (team.get("id"), team["name"], db.js(nets), db.js(chans),
                       db.js(prefs), uid, int(time.time())))
        else:
            c.execute("INSERT INTO passes (kind, series_title, series_guid, networks, "
                      "channels, prefs, uid, enabled, created_at) "
                      "VALUES ('series',?,?,?,?,?,?,1,?)",
                      (series, series, db.js(nets), db.js(chans), db.js(prefs),
                       uid, int(time.time())))
        new_id = c.execute("SELECT last_insert_rowid()").fetchone()[0]
    return new_id, label, True


def _make_ce_rule(row, chosen, nets, chans, prefs=None):
    """Take over a recurring choice that Plex cannot express.

    Plex's team and series rules accept one channel. Several networks, or
    several channels, needs someone to watch the guide and pin each airing, so
    CouchElephant keeps the rule and does that itself.
    """
    title = chosen.get("title") or ""
    # A team template names the team it follows: "All Kansas City Chiefs Events".
    team = None
    for t in db.unjs(row["teams"]) or []:
        if t.get("name") and t["name"] in title:
            team = t
            break
    if team:
        _, label, _ = _make_pass("team", team=team, nets=nets, chans=chans, prefs=prefs)
    else:
        series = row["grandparent_title"] or row["title"]
        _, label, _ = _make_pass("series", series=series, nets=nets, chans=chans,
                                 prefs=prefs)

    done = passes.run_passes()
    made = len([d for d in done if d["action"] == "scheduled"])
    where = " or ".join(nets + chans)
    return JSONResponse({
        "ok": True, "ce_rule": True,
        "message": (f"CouchElephant is following {label}, only from {where}. "
                    f"{made} upcoming airing(s) scheduled."),
    })


@router.post("/api/record")
async def api_record(airing_id: str = Form(...), template: int = Form(0),
                     settings: str = Form(""), networks: str = Form(""),
                     channels: str = Form("")):
    """Schedule this broadcast with the options the user chose.

    A source limit across several networks or channels is something Plex cannot
    express: its own rules take one channel or none. When one is set on a
    recurring choice, CouchElephant keeps the rule itself and pins each airing
    as it comes, rather than handing Plex a rule that would ignore the limit.
    """
    if db.get_setting("dry_run") == "1":
        return JSONResponse({"ok": False, "error":
                             "Preview mode is on. Turn it off in Settings to record."},
                            status_code=400)
    row = _airing_row(airing_id)
    if not row:
        return JSONResponse({"ok": False, "error": "airing not found"}, status_code=404)
    if row["drm"]:
        return JSONResponse({"ok": False, "error":
                             "This airing is DRM encrypted and cannot be recorded."},
                            status_code=400)
    nets = db.unjs(networks, []) or []
    chans = db.unjs(channels, []) or []
    try:
        with _plex() as plex:
            options = await asyncio.to_thread(passes.templates, plex, row)
            if not options:
                raise PlexError("Plex offered no recording options")
            chosen = options[template] if 0 <= template < len(options) else options[0]
            one_shot = (chosen.get("title") or "").lower().startswith("this ")

            prefs = dict(db.unjs(settings, {}) or {})
            prefs = {k: v for k, v in prefs.items() if v is not None}

            if (nets or chans) and not one_shot:
                # The settings the user just filled in belong to the pass too,
                # or padding and quality are silently lost by choosing a
                # source limit.
                return await asyncio.to_thread(_make_ce_rule, row, chosen, nets,
                                               chans, prefs)

            # oneShot is never shown, and it is what separates one game from
            # every future airing of the same programme. It follows the template.
            prefs["oneShot"] = "1" if one_shot else "0"

            await asyncio.to_thread(passes._schedule, plex, row, None, "manual",
                                    chosen, prefs)
            await asyncio.to_thread(sync.sync_recordings, plex)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=500)
    return JSONResponse({"ok": True, "message": "Recording scheduled."})


@router.post("/api/record/cancel")
async def api_record_cancel(airing_id: str = Form(...)):
    """Undo a recording this app scheduled."""
    mine = db.one("SELECT * FROM our_grabs WHERE airing_id = ?", (airing_id,))
    if not mine:
        return JSONResponse({"ok": False, "error": "CouchElephant did not schedule this."},
                            status_code=404)
    key = mine["subscription"]
    with _plex() as plex:
        if not key:
            # Scheduled before the key was being stored, or Plex was slow to
            # list it. Look it up again rather than refusing to cancel.
            try:
                key = await asyncio.to_thread(plex.find_subscription,
                                              mine["program_guid"], mine["begins_at"])
            except Exception:
                key = None
        if not key:
            return JSONResponse({"ok": False, "error":
                                 "Cannot find this recording in Plex. It may already "
                                 "be gone."}, status_code=404)
        try:
            await asyncio.to_thread(plex.delete_subscription, key)
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"},
                                status_code=500)
        await asyncio.to_thread(passes.forget, airing_id)
        await asyncio.to_thread(sync.sync_recordings, plex)
    # Deleting the subscription stops Plex recording, but a recording already
    # under way leaves the part it captured in the library. Say so rather than
    # letting the user find a stray file later.
    started = db.one(
        "SELECT status FROM plex_grabs WHERE channel_vcn = ? AND begins_at = ? "
        "AND status IN ('inprogress','complete') LIMIT 1",
        (mine["channel_vcn"], mine["begins_at"]))
    if started:
        return JSONResponse({"ok": True, "message":
                             "Cancelled. It had already started, so what Plex "
                             "captured so far is still in your library."})
    return JSONResponse({"ok": True, "message": "Recording cancelled."})


@router.post("/api/pass")
async def api_pass(team_id: int = Form(...)):
    """Follow a team from the programme panel.

    It runs the passes straight away, like every other way of creating one.
    Without that the first game was not booked until the next sync, up to an
    hour later, and the panel said "Following" with nothing to show for it.
    """
    t = db.one("SELECT * FROM teams WHERE id = ?", (team_id,))
    if not t:
        return JSONResponse({"ok": False, "error": "unknown team"}, status_code=404)
    _, label, created = await asyncio.to_thread(
        _make_pass, "team", dict(t), None, [], [], {}, False)
    if not created:
        return JSONResponse({"ok": True, "message": f"Already following {label}."})
    done = await asyncio.to_thread(passes.run_passes)
    made = len([d for d in done if d["action"] == "scheduled"])
    return JSONResponse({"ok": True, "message":
                         f"Following {label}. {made} upcoming game(s) scheduled."})


