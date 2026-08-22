"""FastAPI app: guide, search, recordings, passes, settings."""
import asyncio
import datetime
import os
import time
import urllib.parse
import zoneinfo

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db, filters, passes, sync
from .plex import Plex, PlexError

BASE = os.path.dirname(__file__)
templates = Jinja2Templates(directory=os.path.join(BASE, "templates"))
app = FastAPI(title="CouchElephant", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")


# ---------- helpers ----------

def tz():
    try:
        return zoneinfo.ZoneInfo(db.get_setting("timezone") or "UTC")
    except Exception:
        return zoneinfo.ZoneInfo("UTC")


def fmt(ts, pattern="%a %b %d, %-I:%M %p"):
    if not ts:
        return ""
    return datetime.datetime.fromtimestamp(int(ts), tz()).strftime(pattern)


def fmt_day(ts):
    if not ts:
        return ""
    return datetime.datetime.fromtimestamp(int(ts), tz()).strftime("%A %B %-d")


templates.env.filters["fmt"] = fmt
templates.env.filters["fmt_day"] = fmt_day
templates.env.filters["unjs"] = db.unjs


def _channel_list():
    return db.query(
        "SELECT vcn, call_sign AS cs FROM channels WHERE vcn IS NOT NULL "
        "ORDER BY CAST(vcn AS REAL)")


def _logo_map():
    """vcn -> True when a logo is cached, so the template can skip the <img>."""
    return {r["vcn"]: True for r in db.query(
        "SELECT vcn FROM channels WHERE logo_path IS NOT NULL AND logo_path != ''")}


def page(request, name, **ctx):
    ctx.setdefault("settings", db.all_settings())
    ctx.setdefault("configured", bool(db.get_setting("plex_url") and db.get_setting("plex_token")))
    ctx.setdefault("last_sync", db.one("SELECT * FROM sync_log ORDER BY id DESC LIMIT 1"))
    ctx.setdefault("now", int(time.time()))
    ctx["request"] = request
    return templates.TemplateResponse(name, ctx)


# ---------- lifecycle ----------

@app.on_event("startup")
async def startup():
    db.init()
    asyncio.create_task(sync_loop())


async def sync_loop():
    """Periodic guide refresh plus pass evaluation."""
    await asyncio.sleep(3)
    while True:
        try:
            minutes = int(db.get_setting("sync_minutes") or 60)
        except ValueError:
            minutes = 60
        if db.get_setting("plex_url") and db.get_setting("plex_token"):
            ok, detail = await asyncio.to_thread(sync.full_sync)
            if ok:
                await asyncio.to_thread(passes.run_passes)
        await asyncio.sleep(max(5, minutes) * 60)


# ---------- guide ----------

PAGE_SIZE = 80


def _airings_query(day: str, channel: str, sports: int, q: str, offset: int, limit: int,
                   f: str = "", x: str = ""):
    """Rows for the guide, whether that is one day or a search.

    Ordering ends with a.id so paging is deterministic. Without a unique tie
    breaker, two airings sharing a start time can repeat or vanish across pages.
    """
    now = int(time.time())
    sql = ["""SELECT a.*, p.title, p.grandparent_title, p.summary, p.teams, p.section
              FROM airings a JOIN programs p ON p.guid = a.program_guid"""]
    args = []
    if q.strip():
        like = f"%{q.strip()}%"
        sql.append("""WHERE (p.title LIKE ? OR p.grandparent_title LIKE ? OR p.summary LIKE ?)
                        AND a.ends_at > ?""")
        args += [like, like, like, now]
        order = "ORDER BY a.begins_at, CAST(a.channel_vcn AS REAL), a.id"
    else:
        start = now
        if day:
            try:
                d = datetime.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=tz())
                start = int(d.timestamp())
            except ValueError:
                pass
        sql.append("WHERE a.ends_at > ? AND a.begins_at < ?")
        args += [start, start + 86400]
        order = "ORDER BY a.begins_at, CAST(a.channel_vcn AS REAL), a.id"
    if channel:
        sql.append("AND a.channel_vcn = ?")
        args.append(channel)
    if sports:
        sql.append("AND p.section = 'sports'")
    frags, fargs = filters.build(filters.parse(f), filters.parse(x))
    sql += frags
    args += fargs
    sql.append(f"{order} LIMIT ? OFFSET ?")
    args += [limit, offset]
    return db.query(" ".join(sql), tuple(args))


@app.get("/api/grid")
def api_grid(start: int, end: int, choffset: int = 0, chlimit: int = 12,
             sports: int = 0, channel: str = "", f: str = "", x: str = ""):
    """A window of the guide grid: some channels, over some span of time.

    The client asks for more channels when it scrolls down and for more time
    when it scrolls right, so both axes page independently against the same
    endpoint.
    """
    csql = ["SELECT vcn, call_sign, logo_path FROM channels WHERE vcn IS NOT NULL"]
    cargs = []
    if channel:
        csql.append("AND vcn = ?")
        cargs.append(channel)
    if sports:
        # Only channels that actually carry sport in the loaded window.
        csql.append("""AND vcn IN (SELECT a.channel_vcn FROM airings a
                                   JOIN programs p ON p.guid = a.program_guid
                                   WHERE p.section = 'sports')""")
    chosen = [t.split(":", 1)[1] for t in filters.parse(f) if t.startswith("channel:")]
    if chosen:
        csql.append("AND vcn IN (" + ",".join("?" for _ in chosen) + ")")
        cargs += chosen
    dropped = [t.split(":", 1)[1] for t in filters.parse(x) if t.startswith("channel:")]
    if dropped:
        csql.append("AND vcn NOT IN (" + ",".join("?" for _ in dropped) + ")")
        cargs += dropped
    csql.append("ORDER BY CAST(vcn AS REAL) LIMIT ? OFFSET ?")
    cargs += [chlimit + 1, choffset]
    crows = db.query(" ".join(csql), tuple(cargs))

    more_channels = len(crows) > chlimit
    crows = crows[:chlimit]
    vcns = [r["vcn"] for r in crows]
    channels = [{"vcn": r["vcn"], "call_sign": r["call_sign"] or "",
                 "logo": bool(r["logo_path"])} for r in crows]

    ours = {r["airing_id"] for r in db.query("SELECT airing_id FROM our_grabs")}
    plex_titles = {r["title"] for r in db.query(
        "SELECT title FROM plex_grabs WHERE status IN ('scheduled','inprogress')")}

    frags, fargs = filters.build(filters.parse(f), filters.parse(x))
    airings = []
    if vcns:
        marks = ",".join("?" for _ in vcns)
        rows = db.query(
            f"""SELECT a.id, a.channel_vcn, a.begins_at, a.ends_at, a.premiere, a.drm,
                       p.title, p.grandparent_title, p.summary, p.section
                FROM airings a JOIN programs p ON p.guid = a.program_guid
                WHERE a.channel_vcn IN ({marks}) AND a.begins_at < ? AND a.ends_at > ?
                {' '.join(frags)}
                ORDER BY a.channel_vcn, a.begins_at, a.id""",
            tuple(vcns) + (end, start) + tuple(fargs))
        for r in rows:
            if sports and r["section"] != "sports":
                continue
            sched = None
            if r["id"] in ours:
                sched = "ce"
            elif r["title"] in plex_titles:
                sched = "plex"
            airings.append({
                "sched": sched,
                "id": r["id"], "vcn": r["channel_vcn"],
                "b": r["begins_at"], "e": r["ends_at"],
                "premiere": bool(r["premiere"]), "drm": bool(r["drm"]),
                "title": r["title"] or "", "parent": r["grandparent_title"] or "",
                "summary": (r["summary"] or "")[:140],
            })

    # How far the guide itself runs, so the client knows when to stop asking.
    span = db.one("SELECT MIN(begins_at) lo, MAX(ends_at) hi FROM airings")
    return JSONResponse({
        "channels": channels, "airings": airings,
        "more_channels": more_channels,
        "guide_start": span["lo"], "guide_end": span["hi"],
        "start": start, "end": end,
    })


@app.get("/api/facets")
def api_facets():
    return JSONResponse(filters.facets())


@app.get("/api/program")
def api_program(airing_id: str):
    """Everything about one broadcast, plus every other airing of the same
    programme, so the overlay can show which one is live."""
    a = db.one(
        """SELECT a.*, p.title, p.grandparent_title, p.summary, p.teams, p.genres,
                  p.thumb, p.art, p.rating_key, p.originally_available, p.year, p.section
           FROM airings a JOIN programs p ON p.guid = a.program_guid
           WHERE a.id = ?""", (airing_id,))
    if not a:
        return JSONResponse({"error": "not found"}, status_code=404)

    siblings = db.query(
        "SELECT * FROM airings WHERE program_guid = ? ORDER BY begins_at", (a["program_guid"],))
    logos = _logo_map()
    scheduled = db.one(
        "SELECT status FROM plex_grabs WHERE title = ? AND status IN "
        "('scheduled','inprogress','complete') LIMIT 1", (a["title"],))
    ours = {r["airing_id"] for r in db.query(
        "SELECT airing_id FROM our_grabs WHERE program_guid = ?", (a["program_guid"],))}
    my_passes = {r["team_id"] for r in db.query("SELECT team_id FROM passes")}

    teams = db.unjs(a["teams"])
    return JSONResponse({
        "airing_id": a["id"],
        "title": a["title"], "parent": a["grandparent_title"] or "",
        "summary": a["summary"] or "", "year": a["year"],
        "thumb": a["thumb"] or a["art"] or "",
        "genres": db.unjs(a["genres"]),
        "teams": [{"id": t.get("id"), "name": t.get("name"),
                   "followed": t.get("id") in my_passes} for t in teams],
        "section": a["section"],
        "originally_available": a["originally_available"],
        "scheduled": scheduled["status"] if scheduled else None,
        "scheduled_by_us": bool(ours),
        "dry_run": db.get_setting("dry_run") == "1",
        "airings": [{
            "id": r["id"], "vcn": r["channel_vcn"], "call_sign": r["channel_call_sign"] or "",
            "logo": bool(logos.get(r["channel_vcn"])),
            "b": r["begins_at"], "e": r["ends_at"],
            "premiere": bool(r["premiere"]), "drm": bool(r["drm"]),
            "chosen": r["id"] == a["id"],
            "ours": r["id"] in ours,
        } for r in siblings],
    })


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


def _airing_row(airing_id):
    return db.one(
        """SELECT a.*, p.rating_key, p.title, p.grandparent_title, p.teams
           FROM airings a JOIN programs p ON p.guid = a.program_guid
           WHERE a.id = ?""", (airing_id,))


@app.get("/api/record/options")
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
        plex = Plex(db.get_setting("plex_url"), db.get_setting("plex_token"))
        options = await asyncio.to_thread(passes.templates, plex, row)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=502)

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
            # CouchElephant's whole point is pinning the single event to the
            # broadcast the user picked, so those two arrive already set.
            if one_shot and sid == "lineupChannel":
                value = row["channel_identifier"] or value
            if one_shot and sid == "startTimeslot":
                value = str(row["begins_at"])
            settings.append({
                "id": sid, "label": st.get("label"), "type": st.get("type"),
                "value": value, "options": _enum(st.get("enumValues")),
            })
        out.append({
            "index": i, "title": title, "type": s_.get("type"),
            "one_shot": one_shot, "settings": settings,
        })
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


def _make_ce_rule(row, chosen, nets, chans):
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
        existing = db.one("SELECT id FROM passes WHERE kind='team' AND team_id = ?",
                          (team["id"],))
        with db.tx() as c:
            if existing:
                c.execute("UPDATE passes SET networks=?, channels=?, enabled=1 WHERE id=?",
                          (db.js(nets), db.js(chans), existing["id"]))
            else:
                c.execute("INSERT INTO passes (kind, team_id, team_name, networks, "
                          "channels, enabled, created_at) VALUES ('team',?,?,?,?,1,?)",
                          (team["id"], team["name"], db.js(nets), db.js(chans),
                           int(time.time())))
        label = team["name"]
    else:
        series = row["grandparent_title"] or row["title"]
        existing = db.one("SELECT id FROM passes WHERE kind='series' AND series_title = ?",
                          (series,))
        with db.tx() as c:
            if existing:
                c.execute("UPDATE passes SET networks=?, channels=?, enabled=1 WHERE id=?",
                          (db.js(nets), db.js(chans), existing["id"]))
            else:
                c.execute("INSERT INTO passes (kind, series_title, series_guid, networks, "
                          "channels, enabled, created_at) VALUES ('series',?,?,?,?,1,?)",
                          (series, series, db.js(nets), db.js(chans), int(time.time())))
        label = series

    done = passes.run_passes()
    made = len([d for d in done if d["action"] == "scheduled"])
    where = " or ".join(nets + chans)
    return JSONResponse({
        "ok": True, "ce_rule": True,
        "message": (f"CouchElephant is following {label}, only from {where}. "
                    f"{made} upcoming airing(s) scheduled."),
    })


@app.post("/api/record")
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
        plex = Plex(db.get_setting("plex_url"), db.get_setting("plex_token"))
        options = await asyncio.to_thread(passes.templates, plex, row)
        if not options:
            raise PlexError("Plex offered no recording options")
        chosen = options[template] if 0 <= template < len(options) else options[0]
        one_shot = (chosen.get("title") or "").lower().startswith("this ")

        if (nets or chans) and not one_shot:
            return await asyncio.to_thread(_make_ce_rule, row, chosen, nets, chans)

        prefs = dict(db.unjs(settings, {}) or {})
        # oneShot is never shown, and it is what separates one game from every
        # future airing of the same programme. It follows the template.
        prefs["oneShot"] = "1" if one_shot else "0"
        prefs = {k: v for k, v in prefs.items() if v is not None}

        await asyncio.to_thread(passes._schedule, plex, row, None, "manual", chosen, prefs)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=500)
    await asyncio.to_thread(sync.sync_recordings, Plex(db.get_setting("plex_url"),
                                                      db.get_setting("plex_token")))
    return JSONResponse({"ok": True, "message": "Recording scheduled."})


@app.post("/api/record/cancel")
async def api_record_cancel(airing_id: str = Form(...)):
    """Undo a recording this app scheduled."""
    mine = db.one("SELECT * FROM our_grabs WHERE airing_id = ?", (airing_id,))
    if not mine:
        return JSONResponse({"ok": False, "error": "CouchElephant did not schedule this."},
                            status_code=404)
    key = mine["subscription"]
    plex = Plex(db.get_setting("plex_url"), db.get_setting("plex_token"))
    if not key:
        # Scheduled before the key was being stored, or Plex was slow to list
        # it. Look it up again rather than refusing to cancel.
        try:
            key = await asyncio.to_thread(plex.find_subscription,
                                          mine["program_guid"], mine["begins_at"])
        except Exception:
            key = None
    if not key:
        return JSONResponse({"ok": False, "error":
                             "Cannot find this recording in Plex. It may already be gone."},
                            status_code=404)
    try:
        await asyncio.to_thread(plex.delete_subscription, key)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=500)
    await asyncio.to_thread(passes.forget, airing_id)
    await asyncio.to_thread(sync.sync_recordings, plex)
    # Deleting the subscription stops Plex recording, but a recording already
    # under way leaves the part it captured in the library. Say so rather than
    # letting the user find a stray file later.
    started = db.one(
        "SELECT status FROM plex_grabs WHERE title = ? AND status IN "
        "('inprogress','complete') LIMIT 1", (mine["title"],))
    if started:
        return JSONResponse({"ok": True, "message":
                             "Cancelled. It had already started, so what Plex "
                             "captured so far is still in your library."})
    return JSONResponse({"ok": True, "message": "Recording cancelled."})


@app.post("/api/pass")
def api_pass(team_id: int = Form(...)):
    t = db.one("SELECT * FROM teams WHERE id = ?", (team_id,))
    if not t:
        return JSONResponse({"ok": False, "error": "unknown team"}, status_code=404)
    if db.one("SELECT 1 FROM passes WHERE team_id = ?", (team_id,)):
        return JSONResponse({"ok": True, "message": f"Already following {t['name']}."})
    with db.tx() as c:
        c.execute("INSERT INTO passes (kind, team_id, team_name, enabled, created_at) "
                  "VALUES ('team', ?, ?, 1, ?)", (t["id"], t["name"], int(time.time())))
    return JSONResponse({"ok": True, "message": f"Following {t['name']}."})


@app.get("/partial/airings", response_class=HTMLResponse)
def airings_partial(request: Request, day: str = "", channel: str = "", sports: int = 0,
                    q: str = "", offset: int = 0, f: str = "", x: str = ""):
    """Just the rows, for infinite scroll. Empty response means the end."""
    rows = _airings_query(day, channel, sports, q, offset, PAGE_SIZE, f, x)
    if not rows:
        return HTMLResponse("")
    return templates.TemplateResponse(
        "_rows.html", {"request": request, "rows": rows, "q": q, "logos": _logo_map()})


@app.get("/", response_class=HTMLResponse)
def guide(request: Request, day: str = "", channel: str = "", sports: int = 0,
          q: str = "", f: str = "", x: str = ""):
    """Guide and search are the same page.

    A query switches the view from one day's grid to matches across the whole
    guide, because they answer the same question and splitting them into two
    tabs made you navigate to find out what is on.
    """
    now = int(time.time())
    rows = _airings_query(day, channel, sports, q, 0, PAGE_SIZE, f, x)

    days = []
    if not q.strip():
        base = datetime.datetime.fromtimestamp(now, tz()).replace(
            hour=0, minute=0, second=0, microsecond=0)
        for i in range(12):
            d = base + datetime.timedelta(days=i)
            days.append({"key": d.strftime("%Y-%m-%d"),
                         "label": "Today" if i == 0 else d.strftime("%a %-d")})

    return page(request, "guide.html", rows=rows, days=days,
                day=(day or (days[0]["key"] if days else "")),
                channels=_channel_list(), channel=channel, sports=sports,
                q=q, logos=_logo_map(), f=f, x=x,
                nfilters=len(filters.parse(f)) + len(filters.parse(x)))


@app.get("/search")
def search_redirect(q: str = ""):
    """Search used to be its own tab. Keep the URL working."""
    return RedirectResponse(f"/?q={q}" if q else "/", status_code=307)


# ---------- recordings ----------

@app.get("/recordings", response_class=HTMLResponse)
def recordings(request: Request):
    """One page for everything that is set to record.

    A schedule is a schedule whether a team pass, a Plex series rule or a
    single game made it. Splitting them across two tabs asked the user to know
    which machine created a recording before they could find it.
    """
    rules = []

    # Our own team passes. These are rules in the same sense: they keep
    # matching new games without being asked again.
    for p in db.query("SELECT * FROM passes ORDER BY COALESCE(team_name, series_title)"):
        nets = db.unjs(p["networks"]) or []
        chans = db.unjs(p["channels"]) or []
        bits = ["Always from the live broadcast"]
        if nets or chans:
            bits.append("only on " + ", ".join(nets + chans))
        rules.append({
            "kind": "sports" if p["kind"] == "team" else "series",
            "source": "ce", "pass_id": p["id"],
            "enabled": p["enabled"],
            "title": p["team_name"] or p["series_title"],
            "detail": ". ".join(bits),
            "limited": bool(nets or chans),
        })

    # Plex's own rules. A one-shot is a single recording rather than a rule, so
    # it belongs in the list below, not here.
    for s_ in db.query("SELECT * FROM plex_subscriptions ORDER BY title"):
        cfg = db.unjs(s_["settings"], {}) or {}
        if str(cfg.get("oneShot", "")).lower() in ("1", "true", "yes"):
            continue
        bits = []
        bits.append("New only" if cfg.get("onlyNewAirings") == "1" else "New and repeats")
        if cfg.get("lineupChannel"):
            bits.append("one channel")
        if cfg.get("startTimeslot") and cfg.get("startTimeslot") != "-1":
            bits.append("one time")
        rules.append({
            # Plex marks a team pass type 15. Anything else is a series rule.
            "kind": "sports" if str(s_["type"]) == "15" else "series",
            "source": "ce" if s_["owned_by_us"] else "plex",
            "pass_id": None, "enabled": 1, "title": s_["title"],
            "detail": ", ".join(bits), "limited": False,
        })

    # Individual recordings. A grab counts as sport when the programme it comes
    # from carries team tags, which is the same signal the passes work from.
    sports_titles = {r["title"] for r in db.query(
        "SELECT DISTINCT title FROM programs WHERE teams IS NOT NULL AND teams != '[]'")}
    ours = {(g["channel_vcn"], g["begins_at"]) for g in db.query(
        "SELECT channel_vcn, begins_at FROM our_grabs")}
    grabs = []
    for g in db.query("SELECT * FROM plex_grabs ORDER BY COALESCE(begins_at, 0)"):
        d = dict(g)
        d["kind"] = "sports" if g["title"] in sports_titles else "series"
        d["source"] = "ce" if (g["channel_vcn"], g["begins_at"]) in ours else "plex"
        grabs.append(d)

    # What each active pass would do next, and why. This is the part that has
    # to stay visible: a pass that silently picks the wrong broadcast is the
    # bug the whole app exists to fix.
    upcoming = []
    for p in db.query("SELECT * FROM passes WHERE enabled = 1"):
        nets, chans = passes.allowed_sources(p)
        for guid, airings in passes.group_by_game(passes.rule_airings(p)).items():
            allowed = [a for a in airings if passes.in_sources(a, nets, chans)]
            if not allowed:
                continue
            pick, reason = passes.choose_airing(allowed)
            if not pick:
                continue
            if nets or chans:
                reason += ", limited to " + " or ".join(nets + chans)
            upcoming.append({
                "team": passes.rule_label(p), "title": pick["title"],
                "grandparent": pick["grandparent_title"],
                "channel": f'{pick["channel_vcn"]} {pick["channel_call_sign"] or ""}'.strip(),
                "begins_at": pick["begins_at"], "reason": reason,
                "alternatives": len(airings) - 1,
                "blocked": passes.already_handled(guid, pick["id"]),
            })
    upcoming.sort(key=lambda x: x["begins_at"])

    teams = db.query("SELECT * FROM teams ORDER BY name")
    actions = db.query(
        "SELECT a.*, p.team_name FROM pass_actions a LEFT JOIN passes p ON p.id = a.pass_id "
        "ORDER BY a.id DESC LIMIT 60")
    return page(request, "recordings.html", rules=rules, grabs=grabs,
                upcoming=upcoming, teams=teams, actions=actions)


# ---------- passes ----------

@app.get("/passes")
def passes_redirect():
    """Sports passes are no longer a page of their own."""
    return RedirectResponse("/recordings", status_code=301)


@app.post("/passes/add")
def pass_add(team_id: int = Form(...)):
    t = db.one("SELECT * FROM teams WHERE id = ?", (team_id,))
    if t:
        with db.tx() as c:
            c.execute("INSERT INTO passes (kind, team_id, team_name, enabled, created_at) "
                      "VALUES ('team', ?, ?, 1, ?)", (t["id"], t["name"], int(time.time())))
    return RedirectResponse("/recordings", status_code=303)


@app.post("/passes/{pass_id}/delete")
def pass_delete(pass_id: int):
    with db.tx() as c:
        c.execute("DELETE FROM passes WHERE id = ?", (pass_id,))
    return RedirectResponse("/recordings", status_code=303)


@app.post("/passes/{pass_id}/toggle")
def pass_toggle(pass_id: int):
    with db.tx() as c:
        c.execute("UPDATE passes SET enabled = 1 - enabled WHERE id = ?", (pass_id,))
    return RedirectResponse("/recordings", status_code=303)


@app.post("/passes/run")
async def passes_run():
    await asyncio.to_thread(passes.run_passes)
    return RedirectResponse("/recordings", status_code=303)


# ---------- settings ----------

@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, tested: str = ""):
    zones = sorted(z for z in zoneinfo.available_timezones() if "/" in z)
    return page(request, "settings.html", zones=zones, tested=tested,
                logos=sync.logo_coverage())


@app.post("/settings")
def settings_save(plex_url: str = Form(""), plex_token: str = Form(""),
                  timezone: str = Form("UTC"), sync_minutes: str = Form("60"),
                  dry_run: str = Form("0")):
    db.set_setting("plex_url", plex_url.strip().rstrip("/"))
    if plex_token.strip() and not plex_token.startswith("*"):
        db.set_setting("plex_token", plex_token.strip())
    db.set_setting("timezone", timezone)
    db.set_setting("sync_minutes", sync_minutes)
    db.set_setting("dry_run", "1" if dry_run == "1" else "0")
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/test")
def settings_test():
    try:
        plex = Plex(db.get_setting("plex_url"), db.get_setting("plex_token"))
        info = plex.server_info()
        dvrs = plex.dvrs()
        msg = (f"OK: {info.get('friendlyName')} (Plex {info.get('version')}), "
               f"{len(dvrs)} DVR(s)")
    except Exception as e:
        msg = f"FAILED: {type(e).__name__}: {e}"
    return RedirectResponse(f"/settings?tested={msg}", status_code=303)


@app.post("/settings/logos")
async def refetch_logos():
    """Force every channel logo to be downloaded again."""
    await asyncio.to_thread(sync.cache_logos, True)
    return RedirectResponse("/settings", status_code=303)


@app.post("/sync")
async def sync_now():
    await asyncio.to_thread(sync.full_sync)
    await asyncio.to_thread(passes.run_passes)
    return RedirectResponse("/", status_code=303)


# A 1x1 transparent PNG, so a missing logo renders as empty space rather than
# a broken-image glyph.
_BLANK = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6360000002000100ffff03000006000557bfabd4"
    "0000000049454e44ae426082")


@app.get("/logo/{vcn}")
def logo(vcn: str):
    """Channel logo, served from the local cache."""
    row = db.one("SELECT logo_path FROM channels WHERE vcn = ?", (vcn,))
    path = row["logo_path"] if row else None
    if path and os.path.exists(path):
        return FileResponse(path, media_type="image/png",
                            headers={"Cache-Control": "public, max-age=604800"})
    return Response(_BLANK, media_type="image/png",
                    headers={"Cache-Control": "public, max-age=3600"})


@app.get("/healthz")
def healthz():
    last = db.one("SELECT * FROM sync_log ORDER BY id DESC LIMIT 1")
    return JSONResponse({
        "ok": True,
        "configured": bool(db.get_setting("plex_url") and db.get_setting("plex_token")),
        "dry_run": db.get_setting("dry_run") == "1",
        "last_sync": dict(last) if last else None,
        "airings": db.one("SELECT COUNT(*) n FROM airings")["n"],
        "passes": db.one("SELECT COUNT(*) n FROM passes")["n"],
        "logos": sync.logo_coverage(),
    })
