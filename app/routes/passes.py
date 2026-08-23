"""The recordings page: the schedule, passes, rules, smart filters."""
import asyncio
import time

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import db, passes, smartfilter, sync, teamcat
from ..plex import PlexError
from ._shared import _int, _logo_map, _plex, page
from .guide import _PASS_ICON, _why_for  # noqa: F401
from .record import _PASS_HIDDEN, SPORTS_PADDING, _make_pass, _pass_prefs, _template_payload

router = APIRouter()

# ---------- recordings ----------

@router.get("/recordings", response_class=HTMLResponse)
def recordings(request: Request):
    """The shell. Everything on it is fetched from /api/schedule and /api/rules.

    This route used to build the rules, the grabs, the upcoming picks, the
    teams and the decision log on every page load, for a template that stopped
    reading any of them. The upcoming picks alone ran a full pass evaluation
    and threw it away.
    """
    return page(request, "recordings.html")


# ---------- passes ----------

@router.get("/passes")
def passes_redirect():
    """Sports passes are no longer a page of their own."""
    return RedirectResponse("/recordings", status_code=301)


def _why_map():
    """For each broadcast we booked, what booked it.

    Keyed by (channel, start), because that pair names one broadcast and is the
    only thing Plex's own grab list and our record of it share.
    """
    out = {}
    for r in db.query(
            """SELECT o.channel_vcn, o.begins_at, o.source, o.airing_id, o.pass_id,
                      p.kind, p.team_name, p.series_title, p.label, p.enabled
               FROM our_grabs o LEFT JOIN passes p ON p.id = o.pass_id"""):
        name = r["team_name"] or r["series_title"] or r["label"]
        if r["pass_id"] and name:
            out[(r["channel_vcn"], r["begins_at"])] = {
                "who": "ce",
                "kind": _PASS_ICON.get(r["kind"], "series"),
                "reason": f"the {name} pass",
                "pass_id": r["pass_id"],
                "airing_id": r["airing_id"],
            }
        else:
            out[(r["channel_vcn"], r["begins_at"])] = {
                "who": "ce", "kind": "one",
                "reason": "recorded once, by hand",
                "pass_id": None, "airing_id": r["airing_id"],
            }
    return out


def _schedule_rows(limit=None, offset=0, start=None, end=None):
    """The schedule actually in place on the Plex server.

    Read from Plex's own grab list, not from our intentions, because that is
    what will really record. How each one got there is added afterwards.
    """
    where, args = [], []
    if start is not None:
        where.append("COALESCE(g.begins_at, 0) >= ?")
        args.append(start)
    if end is not None:
        where.append("COALESCE(g.begins_at, 0) < ?")
        args.append(end)
    sql = "SELECT g.* FROM plex_grabs g"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY COALESCE(g.begins_at, 0), g.id"
    if limit is not None:
        sql += f" LIMIT {int(limit)} OFFSET {int(offset)}"

    why = _why_map()
    subs = {s["key"]: s["title"] for s in db.query("SELECT key, title FROM plex_subscriptions")}
    sports_titles = {r["title"] for r in db.query(
        "SELECT DISTINCT title FROM programs WHERE teams IS NOT NULL AND teams != '[]'")}
    logos = _logo_map()

    out = []
    for g in db.query(sql, tuple(args)):
        w = why.get((g["channel_vcn"], g["begins_at"]))
        if w:
            who, kind, reason = w["who"], w["kind"], w["reason"]
            pass_id, airing_id = w["pass_id"], w["airing_id"]
        else:
            who, pass_id, airing_id = "plex", None, None
            title = subs.get(g["subscription"] or "")
            kind = "sports" if g["title"] in sports_titles else "series"
            reason = f"a Plex rule, {title}" if title and not title.startswith("This ") \
                else "scheduled in Plex"
        # Match the grab back to a broadcast in the guide, so clicking it opens
        # the same panel the guide opens.
        if not airing_id and g["begins_at"]:
            a = db.one("SELECT id FROM airings WHERE channel_vcn = ? AND begins_at = ? "
                       "LIMIT 1", (g["channel_vcn"], g["begins_at"]))
            airing_id = a["id"] if a else None
        out.append({
            "id": g["id"], "title": g["title"], "parent": g["parent_title"] or "",
            "vcn": g["channel_vcn"] or "", "logo": bool(logos.get(g["channel_vcn"])),
            "b": g["begins_at"], "e": g["ends_at"], "status": g["status"],
            "who": who, "kind": kind, "reason": reason,
            "pass_id": pass_id, "airing_id": airing_id,
        })
    return out


@router.get("/api/schedule")
def api_schedule(offset: int = 0, limit: int = 40, start: int = 0, end: int = 0):
    rows = _schedule_rows(limit=limit, offset=offset,
                          start=start or None, end=end or None)
    # Counted over the same window as the rows. Counting every grab while the
    # rows were windowed made `more` true forever for a windowed query.
    where, args = [], []
    if start:
        where.append("COALESCE(begins_at, 0) >= ?")
        args.append(start)
    if end:
        where.append("COALESCE(begins_at, 0) < ?")
        args.append(end)
    sql = "SELECT COUNT(*) c FROM plex_grabs"
    if where:
        sql += " WHERE " + " AND ".join(where)
    total = db.one(sql, tuple(args))["c"]
    return JSONResponse({"ok": True, "rows": rows, "total": total,
                         "more": offset + len(rows) < total})


@router.get("/api/series")
def api_series(q: str = ""):
    """Programmes in the guide that a rule could follow.

    Grouped by the show rather than the episode, because a rule follows the
    series. Only what is actually coming up, so the list cannot offer something
    the guide can no longer record.
    """
    q = (q or "").strip()
    like = f"%{smartfilter.like(q)}%"
    rows = db.query(
        """SELECT COALESCE(NULLIF(p.grandparent_title,''), p.title) AS name,
                  COUNT(DISTINCT a.id) AS airings,
                  MIN(a.begins_at) AS next_at
           FROM airings a JOIN programs p ON p.guid = a.program_guid
           WHERE a.begins_at > ? AND (? = '' OR name LIKE ? ESCAPE '\\' COLLATE NOCASE)
           GROUP BY name ORDER BY airings DESC, name LIMIT 40""",
        (int(time.time()), q, like))
    return JSONResponse({"ok": True, "series": [dict(r) for r in rows]})


TEAM_PAGE = 300


@router.get("/api/teams")
def api_teams(q: str = "", league: str = "", playing: int = 0):
    """Every team you could follow: the shipped catalogue and Plex's own list.

    Plex knows only the teams playing inside its guide window, about eleven
    days. That was 76 teams on a real server, so "follow the Chiefs" in June
    found nothing. The catalogue in app/data/teams.json carries the rest, and
    the two are merged here.

    A team Plex has seen has an id and works at once. One that is only in the
    catalogue has no id yet; following it is allowed, and the pass waits for
    the team to appear rather than pretending to run.
    """
    q = (q or "").strip()
    nq = teamcat.norm(q)

    followed_ids, followed_names = set(), set()
    for r in db.query("SELECT team_id, team_name FROM passes WHERE kind = 'team'"):
        if r["team_id"]:
            followed_ids.add(r["team_id"])
        if r["team_name"]:
            followed_names.add(teamcat.norm(r["team_name"]))

    out, seen = [], set()
    # Plex's own first: these have ids, so they can be followed today.
    for r in db.query("SELECT id, name, league, in_guide FROM teams ORDER BY name"):
        entry = teamcat.find(r["name"])
        key = teamcat.norm(r["name"])
        seen.add(key)
        out.append({
            "id": r["id"], "name": r["name"],
            "league": r["league"] or (entry["league"] if entry else ""),
            "sports": entry["sports"] if entry else [],
            "playing": bool(r["in_guide"]),
            "followed": r["id"] in followed_ids or key in followed_names,
        })
    for t in teamcat.all_teams():
        key = teamcat.norm(t["name"])
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "id": None, "name": t["name"], "league": t["league"],
            "sports": t.get("sports") or [], "playing": False,
            "followed": key in followed_names,
        })

    total = len(out)
    if league:
        out = [t for t in out if t["league"] == league]
    if playing:
        out = [t for t in out if t["playing"]]
    if q:
        # Matched on the normalised name as well as the raw one, so "st louis"
        # finds "St. Louis" and "bayern munchen" finds "Bayern Munich".
        low = q.lower()
        out = [t for t in out
               if low in t["name"].lower() or (nq and nq in teamcat.norm(t["name"]))]

    # In the guide first: those play this week, and are usually what is meant.
    out.sort(key=lambda t: (not t["playing"], t["name"]))
    return JSONResponse({
        "ok": True, "total": total, "matched": len(out),
        "leagues": teamcat.leagues(),
        "playing_now": sum(1 for t in out if t["playing"]),
        "more": max(0, len(out) - TEAM_PAGE),
        "teams": out[:TEAM_PAGE],
    })


@router.get("/api/sources")
def api_sources():
    """Networks and channels a rule can be limited to."""
    nets = {}
    chans = []
    for c in db.query("SELECT vcn, call_sign, network FROM channels "
                      "ORDER BY CAST(vcn AS REAL)"):
        if c["network"]:
            nets.setdefault(c["network"], []).append(c["vcn"])
        chans.append({"vcn": c["vcn"], "call_sign": c["call_sign"] or "",
                      "network": c["network"] or ""})
    return JSONResponse({
        "ok": True,
        "networks": [{"name": n, "channels": v} for n, v in
                     sorted(nets.items(), key=lambda kv: kv[0].lower())],
        "channels": chans,
    })


@router.get("/api/rules/options")
def api_rule_options(kind: str = "team", team_id: str = "", series: str = "",
                     filter: str = "", ce_pass: int = 0, sporty: int = 0):
    """Plex's own options for a rule that follows this team, programme or filter.

    A template belongs to a programme, so one upcoming broadcast stands in for
    the rest.

    `ce_pass` asks for the settings a CouchElephant pass will actually use,
    which are the one-shot template's, because that is what it books for each
    airing. Without it the recurring choices are offered, which is right when
    the rule is about to become Plex's own.
    """
    if kind == "any":
        # No target yet: a filter with nothing in it, or a team that has not
        # played. Plex offers the same settings whatever the programme, so
        # there is no reason to make somebody choose before they can see them.
        rows = passes.any_airing()
    elif kind == "smart":
        tree = db.unjs(filter, None)
        rows = []
        if tree:
            try:
                rows = passes.smart_airings(tree)
            except smartfilter.FilterError:
                rows = []
        # A filter matching nothing still gets the settings. They belong to
        # Plex, not to the filter.
        rows = rows or passes.any_airing()
    elif kind == "team":
        rows = passes.candidate_airings(_int(team_id)) or (
            passes.any_airing() if ce_pass else [])
    else:
        rows = passes.series_airings((series or "").strip()) or (
            passes.any_airing() if ce_pass else [])
    if not rows:
        return JSONResponse({"ok": False, "error":
                             "Nothing from this is in the guide yet, so Plex has no "
                             "options to offer for it."})
    row = rows[0]
    try:
        with _plex() as plex:
            options = passes.templates(plex, row)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"})

    every = _template_payload(options, row, pin=False)
    if ce_pass:
        # The one-shot, minus what only a recurring rule can honour.
        payload = [dict(t, settings=[s2 for s2 in t["settings"]
                                     if s2["id"] not in _PASS_HIDDEN])
                   for t in every if t["one_shot"]]
        for t in payload:
            t["title"] = "Every airing this pass books"
    else:
        payload = [t for t in every if not t["one_shot"]]
    # Put the one that names what you chose first, since that is the rule the
    # user means; Plex lists them in its own order.
    if kind == "team":
        t = db.one("SELECT name FROM teams WHERE id = ?", (team_id or 0,))
        want = t["name"] if t else ""
    else:
        want = (series or "").strip()
    if want and not ce_pass:
        payload.sort(key=lambda t: 0 if want.lower() in (t["title"] or "").lower() else 1)
    # A pass that follows a team follows sport, and sport runs over.
    #
    # `sporty` is also accepted from the caller, because the panel knows it is
    # on the sports-team route before a team has been chosen. Without that the
    # answer depended on whatever airing happened to stand in, so the padding
    # was filled in on one server and not on another.
    is_sport = (kind == "team" or sporty
                or (row["section"] if "section" in row.keys() else "") == "sports")
    return JSONResponse({"ok": True, "templates": payload, "sample": row["title"],
                         "sporty": bool(is_sport),
                         "sports_padding": SPORTS_PADDING})


@router.get("/api/rules")
def api_rules():
    """Every pass, with what it follows and where it may record from."""
    out = []
    for p in db.query("SELECT * FROM passes "
                      "ORDER BY COALESCE(team_name, series_title, label)"):
        nets = db.unjs(p["networks"]) or []
        chans = db.unjs(p["channels"]) or []
        booked = db.one("SELECT COUNT(*) c FROM our_grabs WHERE pass_id = ?", (p["id"],))["c"]
        tree = db.unjs(p["filter"], None) if p["kind"] == "smart" else None
        out.append({
            "id": p["id"], "who": "ce", "kind": p["kind"],
            "icon": {"team": "sports", "smart": "smart"}.get(p["kind"], "series"),
            "title": passes.rule_label(p),
            "team_id": p["team_id"], "series": p["series_title"],
            "filter": tree,
            "networks": nets, "channels": chans,
            "prefs": db.unjs(p["prefs"], {}) or {},
            "enabled": bool(p["enabled"]), "booked": booked,
            "waiting": p["kind"] == "team" and not p["team_id"],
            "detail": {
                "team": ("Every game, from the live broadcast" if p["team_id"] else
                         "Waiting for this team to appear in the guide"),
                "series": "Every episode the guide carries",
                "smart": ("Anything matching " + smartfilter.describe(tree or {})),
            }.get(p["kind"], ""),
        })

    # Plex's own recurring rules belong here too. A rule you just made would
    # otherwise be invisible until Plex got round to scheduling something.
    sports_titles = {r["title"] for r in db.query(
        "SELECT DISTINCT title FROM programs WHERE teams IS NOT NULL AND teams != '[]'")}
    for s_ in db.query("SELECT * FROM plex_subscriptions ORDER BY title"):
        cfg = db.unjs(s_["settings"], {}) or {}
        if str(cfg.get("oneShot", "")).lower() in ("1", "true", "yes"):
            continue
        booked = db.one("SELECT COUNT(*) c FROM plex_grabs WHERE subscription = ?",
                        (s_["key"],))["c"]
        bits = [s_["title"]] if s_["target"] else []
        bits.append("New only" if cfg.get("onlyNewAirings") == "1" else "New and repeats")
        if cfg.get("lineupChannel"):
            bits.append("one channel")
        out.append({
            "id": s_["key"], "who": "plex",
            "kind": "team" if str(s_["type"]) == "15" else "series",
            "icon": "sports" if (str(s_["type"]) == "15"
                                 or s_["title"] in sports_titles) else "series",
            "title": s_["target"] or s_["title"],
            "team_id": None, "series": None,
            "networks": [], "channels": [], "enabled": True, "booked": booked,
            "detail": ", ".join(bits),
        })
    return JSONResponse({"ok": True, "rules": out})


@router.get("/api/rules/{rule_id}/upcoming")
def api_rule_upcoming(rule_id: int):
    """What this pass will record next, and why it picked each one.

    The same reasoning the pass itself uses, run live rather than remembered,
    so the answer matches what would happen if it ran right now.
    """
    r = db.one("SELECT * FROM passes WHERE id = ?", (rule_id,))
    if not r:
        return JSONResponse({"ok": False, "error": "no such pass"}, status_code=404)
    nets, chans = passes.allowed_sources(r)
    logos = _logo_map()
    out = []
    for guid, airings in passes.group_by_game(passes.rule_airings(r)).items():
        allowed = [a for a in airings if passes.in_sources(a, nets, chans)]
        if not allowed:
            out.append({
                "title": airings[0]["title"],
                "parent": airings[0]["grandparent_title"] or "",
                "b": airings[0]["begins_at"], "vcn": "", "call_sign": "",
                "logo": False, "airing_id": None,
                "reason": "no airing is on " + " or ".join(nets + chans),
                "rejected": len(airings), "status": "skipped",
            })
            continue
        pick, reason = passes.choose_airing(allowed)
        if not pick:
            continue
        if nets or chans:
            reason += ", limited to " + " or ".join(nets + chans)
        out.append({
            "title": pick["title"], "parent": pick["grandparent_title"] or "",
            "b": pick["begins_at"], "vcn": pick["channel_vcn"],
            "call_sign": pick["channel_call_sign"] or "",
            "logo": bool(logos.get(pick["channel_vcn"])),
            "airing_id": pick["id"], "reason": reason,
            "rejected": len(airings) - 1,
            "status": passes.already_handled(guid) or "will schedule",
        })
    out.sort(key=lambda x: x["b"] or 0)
    return JSONResponse({"ok": True, "title": passes.rule_label(r), "upcoming": out})


@router.post("/api/rules/{rule_id}")
async def api_rule_edit(rule_id: int, networks: str = Form(""),
                        channels: str = Form(""), enabled: str = Form(""),
                        settings: str = Form(""), filter: str = Form(""),
                        name: str = Form("")):
    """Change a pass: where it may record from, its Plex settings, or pause it."""
    r = db.one("SELECT * FROM passes WHERE id = ?", (rule_id,))
    if not r:
        return JSONResponse({"ok": False, "error": "no such pass"}, status_code=404)
    nets = db.unjs(networks, []) or []
    chans = db.unjs(channels, []) or []
    # An empty settings field means "not sent", not "clear them".
    prefs = db.unjs(settings, None) if settings else None
    keep = db.js(_pass_prefs(prefs)) if prefs is not None else r["prefs"]

    tree = db.unjs(filter, None) if filter else None
    if tree is not None:
        if r["kind"] != "smart":
            return JSONResponse({"ok": False, "error":
                                 "only a smart pass has conditions"}, status_code=400)
        try:
            smartfilter.build(tree)          # refuse to store what cannot run
        except smartfilter.FilterError as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    filt = db.js(tree) if tree is not None else r["filter"]
    label = (name or "").strip() or (r["label"] if "label" in r.keys() else None)
    if tree is not None and not (name or "").strip():
        label = smartfilter.describe(tree)

    # Not sent means unchanged. A settings save must not quietly resume a
    # paused pass.
    on = r["enabled"] if enabled == "" else (0 if enabled in ("0", "false") else 1)
    with db.tx() as c:
        c.execute("UPDATE passes SET networks=?, channels=?, prefs=?, filter=?, "
                  "label=?, enabled=? WHERE id=?",
                  (db.js(nets), db.js(chans), keep, filt, label, on, rule_id))
    done = await asyncio.to_thread(passes.run_passes)
    made = len([d for d in done if d["action"] == "scheduled"])
    where = " or ".join(nets + chans)
    return JSONResponse({"ok": True, "message":
                         (f"Saved. Only from {where}. " if where else "Saved. ")
                         + f"{made} new airing(s) scheduled."})


SMART_LIMIT = 40          # airings a smart filter may book without being told twice


@router.get("/api/filter/fields")
def api_filter_fields():
    """What a smart filter can ask about, and the answers worth offering.

    The panel is built from this rather than from a copy of it, so a field
    added here appears in the UI without anyone editing two places.
    """
    def distinct(sql):
        return [r[0] for r in db.query(sql) if r[0]]

    genres = set()
    for r in db.query("SELECT genres FROM programs WHERE genres IS NOT NULL"):
        for g in db.unjs(r["genres"]):
            if g:
                genres.add(g)

    values = {
        "genres": sorted(genres),
        "ratings": distinct("SELECT DISTINCT content_rating FROM programs "
                            "WHERE content_rating IS NOT NULL AND content_rating != '' "
                            "ORDER BY content_rating"),
        "kinds": [{"value": v, "label": label} for v, label in smartfilter.KINDS],
        "weekdays": [{"value": v, "label": label} for v, label in smartfilter.WEEKDAYS],
        "channels": distinct("SELECT DISTINCT vcn FROM channels ORDER BY "
                             "CAST(vcn AS REAL)"),
        "networks": distinct("SELECT DISTINCT network FROM channels "
                             "WHERE network IS NOT NULL ORDER BY network"),
    }
    fields = [{"id": fid, "label": f["label"], "kind": f["kind"],
               "values": f.get("values")}
              for fid, f in smartfilter.FIELDS.items()]
    # How much of the guide can answer this field at all. A filter on content
    # rating behaves very differently when a third of the guide has none.
    total = db.one("SELECT COUNT(*) c FROM programs")["c"] or 0
    have = db.one("SELECT COUNT(*) c FROM programs WHERE content_rating IS NOT NULL "
                  "AND content_rating != ''")["c"] or 0
    return JSONResponse({
        "ok": True, "fields": fields,
        "comparisons": {k: [{"value": c, "label": label} for c, label in v]
                        for k, v in smartfilter.COMPARISONS.items()},
        "values": values,
        "coverage": {"programs": total, "rated": have},
        "limit": SMART_LIMIT,
    })


@router.post("/api/filter/preview")
async def api_filter_preview(filter: str = Form(...), networks: str = Form(""),
                             channels: str = Form("")):
    """What this filter would record, before anybody commits to it.

    A filter is not obviously loose when you write it. "Genre is Comedy" reads
    like one thing and books hundreds, against a real DVR, on a real disk. So
    the count comes back before the Create button will do anything.
    """
    tree = db.unjs(filter, None)
    if not tree:
        return JSONResponse({"ok": False, "error": "add a condition first"},
                            status_code=400)
    nets = db.unjs(networks, []) or []
    chans = db.unjs(channels, []) or []
    try:
        rows = await asyncio.to_thread(passes.smart_airings, tree)
    except smartfilter.FilterError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    allowed = [r for r in rows if passes.in_sources(r, nets, chans)]
    games = passes.group_by_game(allowed)
    logos = _logo_map()
    sample = []
    for airings in games.values():
        pick, reason = passes.choose_airing(airings)
        if not pick:
            continue
        sample.append({
            "title": pick["title"], "parent": pick["grandparent_title"] or "",
            "b": pick["begins_at"], "vcn": pick["channel_vcn"],
            "call_sign": pick["channel_call_sign"] or "",
            "logo": bool(logos.get(pick["channel_vcn"])), "reason": reason,
        })
    sample.sort(key=lambda x: x["b"] or 0)
    count = len(sample)

    warn = ""
    if count > SMART_LIMIT:
        warn = (f"This would schedule {count} recordings in the next 30 days. "
                "That is a lot of disk and a lot of guide. Narrow it, or say "
                "you meant it.")
    elif smartfilter.is_loose(tree):
        warn = ("Nothing here narrows by what the programme is, only by where "
                "or when it airs, so this will match most of the guide as it "
                "fills up.")
    elif count == 0:
        warn = "Nothing in the guide matches this yet."

    return JSONResponse({
        "ok": True, "count": count, "airings": len(allowed),
        "over": count > SMART_LIMIT, "loose": smartfilter.is_loose(tree),
        "limit": SMART_LIMIT, "warning": warn,
        "describes": smartfilter.describe(tree),
        "sample": sample[:25],
    })


@router.post("/api/rules")
async def api_rule_create(kind: str = Form("team"), team_id: str = Form(""),
                          series: str = Form(""), networks: str = Form(""),
                          channels: str = Form(""), template: int = Form(0),
                          settings: str = Form(""), filter: str = Form(""),
                          name: str = Form(""), confirm: str = Form(""),
                          team: str = Form("")):
    """Create a schedule that keeps matching new airings.

    With no source limit this is something Plex can express on its own, so it
    becomes a plain Plex rule and Plex takes it from there. Name more than one
    network or channel and Plex cannot say it, so CouchElephant keeps the rule
    and books each airing itself.
    """
    nets = db.unjs(networks, []) or []
    chans = db.unjs(channels, []) or []
    prefs = dict(db.unjs(settings, {}) or {})

    if kind == "smart":
        return await _make_smart_rule(filter, name, nets, chans, prefs, confirm)

    if kind == "team":
        t = db.one("SELECT * FROM teams WHERE id = ?", (team_id or 0,))
        if t:
            label, rows = t["name"], passes.candidate_airings(t["id"])
        else:
            # Picked from the catalogue, and not yet in the guide. Plex has no
            # id for it and no rule it could hold, so CouchElephant keeps this
            # one and starts the moment the team turns up.
            wanted = (team or "").strip()
            if not wanted or not teamcat.find(wanted):
                return JSONResponse({"ok": False, "error": "pick a team"},
                                    status_code=400)
            _, label, created = await asyncio.to_thread(
                _make_pass, "team", {"id": None, "name": wanted}, None,
                nets, chans, prefs, False)
            if not created:
                return JSONResponse({"ok": False,
                                     "error": f"You already follow {label}."},
                                    status_code=409)
            return JSONResponse({"ok": True, "ce_rule": True, "waiting": True,
                                 "message":
                                 f"Following {label}. It is not in the guide yet, so "
                                 "nothing is scheduled. CouchElephant starts booking "
                                 "its games the moment it appears."})
    else:
        label = (series or "").strip()
        if not label:
            return JSONResponse({"ok": False, "error": "pick a programme"}, status_code=400)
        rows = passes.series_airings(label)

    if not (nets or chans):
        return await asyncio.to_thread(_make_plex_rule, label, rows, template, prefs)

    _, label, created = await asyncio.to_thread(
        _make_pass, kind,
        dict(t) if kind == "team" else None,
        None if kind == "team" else label,
        nets, chans, prefs, False)
    if not created:
        return JSONResponse({"ok": False, "error": f"You already follow {label}."},
                            status_code=409)

    done = await asyncio.to_thread(passes.run_passes)
    made = len([d for d in done if d["action"] == "scheduled"])
    where = " or ".join(nets + chans)
    return JSONResponse({"ok": True, "ce_rule": True, "message":
                         f"CouchElephant is following {label}, only from {where}. "
                         f"{made} upcoming airing(s) scheduled."})


async def _make_smart_rule(filter_json, name, nets, chans, prefs, confirm):
    """A smart filter is always CouchElephant's to run.

    Plex rules follow one programme or one team. A condition tree is not
    something they can be told, so there is no Plex equivalent to hand this to
    and no decision to make about who owns it.
    """
    tree = db.unjs(filter_json, None)
    if not tree:
        return JSONResponse({"ok": False, "error": "add a condition first"},
                            status_code=400)
    try:
        rows = await asyncio.to_thread(passes.smart_airings, tree)
    except smartfilter.FilterError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)

    allowed = [r for r in rows if passes.in_sources(r, nets, chans)]
    count = len(passes.group_by_game(allowed))
    said_yes = str(confirm).lower() in ("1", "true", "yes")
    # A big filter is not refused, it is questioned. The user gets the number
    # and says it again, which is the difference between a guard and a wall.
    if (count > SMART_LIMIT or smartfilter.is_loose(tree)) and not said_yes:
        return JSONResponse({
            "ok": False, "needs_confirm": True, "count": count,
            "limit": SMART_LIMIT, "loose": smartfilter.is_loose(tree),
            "error": (f"This filter matches {count} programmes in the next 30 days "
                      "and would book every one of them. Press Create again to "
                      "confirm that is what you want."),
        }, status_code=409)

    pass_id, label, created = await asyncio.to_thread(
        _make_pass, "smart", None, None, nets, chans, prefs, False, tree, name)
    if not created:
        return JSONResponse({"ok": False, "error":
                             f"You already have a smart pass for {label}."},
                            status_code=409)

    done = await asyncio.to_thread(passes.run_passes)
    made = len([d for d in done if d["action"] == "scheduled"])
    where = " or ".join(nets + chans)
    return JSONResponse({"ok": True, "ce_rule": True, "id": pass_id, "message":
                         f"Smart pass \u201c{label}\u201d created"
                         + (f", only from {where}" if where else "")
                         + f". {made} upcoming airing(s) scheduled."})


def _make_plex_rule(label, rows, template, prefs):
    """Hand the rule to Plex, which is all it needs when nothing narrows it.

    `template` is Plex's own index into the full template list, the `index`
    each payload entry carries. It is NOT a position in the list the panel
    showed, which is sorted to put the named team first. Indexing the sorted
    list by position once turned "follow the Chiefs" into a rule for every
    NFL game, because Plex lists the league before the team.
    """
    if not rows:
        return JSONResponse({"ok": False, "error":
                             f"Nothing from {label} is in the guide yet, so there is "
                             "nothing for Plex to make a rule from."}, status_code=400)
    row = rows[0]
    try:
        with _plex() as plex:
            every = passes.templates(plex, row)
            recurring = [t for t in every
                         if not (t.get("title") or "").lower().startswith("this ")]
            if not recurring:
                raise PlexError("Plex offered no recurring rule for this")
            chosen = every[template] if 0 <= template < len(every) else None
            if chosen is None or chosen not in recurring:
                chosen = recurring[0]
            prefs = dict(prefs)
            prefs["oneShot"] = "0"
            key = plex.create_recording(
                chosen["parameters"],
                chosen.get("targetLibrarySectionID") or 2,
                int(chosen.get("type") or 2), prefs)
            # `is False` on purpose: None means the check failed, not that the
            # rule is gone.
            if key and plex.subscription_exists(key) is False:
                raise PlexError("Plex accepted the rule and then discarded it. It "
                                "may already have one for this.")
            sync.sync_recordings(plex)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=500)
    return JSONResponse({"ok": True, "ce_rule": False, "message":
                         f"Plex is now recording {chosen.get('title') or label}. "
                         "It appears in the schedule as Plex's own rule."})


@router.post("/api/plexrule/{key}/delete")
def api_plexrule_delete(key: str):
    """Remove one of Plex's own recurring rules."""
    try:
        with _plex() as plex:
            plex.delete_subscription(key)
            sync.sync_recordings(plex)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=500)
    return JSONResponse({"ok": True, "message": "Removed from Plex."})


@router.post("/passes/{pass_id}/delete")
def pass_delete(pass_id: int):
    with db.tx() as c:
        c.execute("DELETE FROM passes WHERE id = ?", (pass_id,))
    return RedirectResponse("/recordings", status_code=303)


@router.post("/passes/{pass_id}/toggle")
def pass_toggle(pass_id: int):
    with db.tx() as c:
        c.execute("UPDATE passes SET enabled = 1 - enabled WHERE id = ?", (pass_id,))
    return RedirectResponse("/recordings", status_code=303)


@router.post("/passes/run")
async def passes_run():
    await asyncio.to_thread(passes.run_passes)
    return RedirectResponse("/recordings", status_code=303)


