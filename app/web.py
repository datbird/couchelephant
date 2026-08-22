"""FastAPI app: guide, search, recordings, passes, settings."""
import asyncio
import datetime
import os
import time
import urllib.parse
import zoneinfo

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import auth, cf_access, db, filters, passes, sync
from .plex import Plex, PlexError

BASE = os.path.dirname(__file__)
templates = Jinja2Templates(directory=os.path.join(BASE, "templates"))
VERSION = "0.90"

# Walking the tzdata directory on every settings render costs more than the
# list is worth. It cannot change while the process runs.
ZONES = sorted(z for z in zoneinfo.available_timezones() if "/" in z)

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


def _logo_map():
    """vcn -> True when a logo is cached, so the template can skip the <img>."""
    return {r["vcn"]: True for r in db.query(
        "SELECT vcn FROM channels WHERE logo_path IS NOT NULL AND logo_path != ''")}


def current_user(request):
    """Who is asking, by whichever route the install allows.

    With sign-in off there is no user, and everything is public. That is the
    state a fresh install starts in.

    Resolved once per request and kept on the request, because the gate and
    the page render both ask, and in Cloudflare mode each ask verifies a JWT.
    """
    cached = getattr(request.state, "ce_user", _MISSING)
    if cached is not _MISSING:
        return cached
    user = _resolve_user(request)
    try:
        request.state.ce_user = user
    except Exception:
        pass
    return user


_MISSING = object()


def _resolve_user(request):
    m = auth.mode()
    if m == "none":
        return None
    user = auth.session_user(request.cookies.get(auth.SESSION_COOKIE))
    if user:
        return user
    if m == "cloudflare":
        token = (request.headers.get("Cf-Access-Jwt-Assertion")
                 or request.cookies.get("CF_Authorization"))
        email = cf_access.verify_email(token, db.get_setting("cf_team_domain"),
                                       db.get_setting("cf_aud"))
        if email:
            return auth.user_for_email(email)
    return None


def page(request, name, **ctx):
    user = ctx.get("user") or current_user(request)
    ctx.setdefault("user", user)
    # A signed-in person's theme is theirs, so it follows them to any browser.
    # Signed out there is nobody to attach it to and the browser's own copy is
    # the only record.
    ctx.setdefault("theme", auth.get_pref(user["id"], "theme") if user else None)
    ctx.setdefault("auth_mode", auth.mode())
    ctx.setdefault("settings", db.all_settings())
    ctx.setdefault("configured", bool(db.get_setting("plex_url") and db.get_setting("plex_token")))
    ctx.setdefault("last_sync", db.one("SELECT * FROM sync_log ORDER BY id DESC LIMIT 1"))
    ctx.setdefault("now", int(time.time()))
    ctx.setdefault("version", VERSION)
    ctx["request"] = request
    return templates.TemplateResponse(name, ctx)


# Reachable without signing in: the sign-in screens themselves, the health
# check, and the static files that render them.
_OPEN_EXACT = frozenset(("/login", "/setup", "/logout", "/healthz", "/welcome",
                         "/favicon.ico"))
_OPEN_PREFIX = ("/static/",)


def _is_open(path):
    """A prefix match let /loginanything past the gate. Match the path itself."""
    return path in _OPEN_EXACT or path.startswith(_OPEN_PREFIX)


@app.middleware("http")
async def _same_origin(request: Request, call_next):
    """Refuse a state-changing request that came from another site.

    With sign-in off, which is the default, nothing else stands between a page
    the user happens to be visiting and this app. That page can POST to
    http://<lan-ip>:8710/settings from their browser, and with no cookie
    required there is nothing to withhold. One such request could null the Plex
    address.

    A browser always sends Origin on a cross-site POST, so comparing it to the
    host this request arrived on is enough, and needs no tokens or session. A
    request with neither header is not from a browser form, so it passes: that
    is curl, and curl was never the threat.
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return await call_next(request)

    origin = request.headers.get("origin") or request.headers.get("referer")
    if origin:
        try:
            sent = urllib.parse.urlsplit(origin).netloc.lower()
        except Exception:
            sent = ""
        here = (request.headers.get("host") or "").lower()
        if sent and here and sent != here:
            return JSONResponse(
                {"ok": False,
                 "error": "That request came from another site and was refused."},
                status_code=403)
    return await call_next(request)


@app.middleware("http")
async def _first_run(request: Request, call_next):
    """Nothing works before there is a server to talk to, so say so once,
    on a screen that asks for it, rather than a banner on every page."""
    path = request.url.path
    if (not _is_open(path) and not path.startswith("/settings")
            and not path.startswith("/api/") and not path.startswith("/partial/")
            and request.method == "GET" and not _configured()):
        return RedirectResponse("/welcome", status_code=303)
    return await call_next(request)


@app.middleware("http")
async def _auth_gate(request: Request, call_next):
    path = request.url.path
    if auth.mode() == "none" or _is_open(path):
        return await call_next(request)
    if current_user(request):
        return await call_next(request)
    # A fresh switch to local sign-in has no accounts yet, so send the first
    # visitor to create one rather than to a login they cannot pass.
    where = "/setup" if auth.needs_setup() else "/login"
    if path.startswith("/api/") or path.startswith("/partial/"):
        return JSONResponse({"ok": False, "error": "sign in required"}, status_code=401)
    return RedirectResponse(where, status_code=303)


# ---------- first run ----------

def _configured():
    return bool(db.get_setting("plex_url") and db.get_setting("plex_token"))


@app.get("/welcome", response_class=HTMLResponse)
def welcome(request: Request):
    if _configured():
        return RedirectResponse("/", status_code=303)
    return page(request, "welcome.html", zones=ZONES, nav="")


@app.post("/welcome")
def welcome_save(plex_url: str = Form(""), plex_token: str = Form(""),
                 timezone: str = Form("UTC")):
    """Test first, then save. A wrong address saved quietly is what makes the
    first five minutes confusing."""
    url, token = plex_url.strip().rstrip("/"), plex_token.strip()
    if not url or not token:
        return JSONResponse({"ok": False,
                             "detail": "Both the address and the token are needed."})
    # Test the candidates without saving them. Writing first and reverting on
    # failure left a window for the sync loop to run against them.
    ok, detail = _test_plex(url, token)
    if not ok:
        return JSONResponse({"ok": False, "detail": detail})
    db.set_setting("plex_url", url)
    db.set_setting("plex_token", token)
    db.set_setting("timezone", timezone)
    db.set_setting("dry_run", "1")
    return JSONResponse({"ok": True, "detail": detail})


# ---------- sign in ----------

def _set_session(resp, token):
    resp.set_cookie(auth.SESSION_COOKIE, token, httponly=True, samesite="lax",
                    max_age=auth.SESSION_TTL, path="/")
    return resp


@app.get("/setup", response_class=HTMLResponse)
def setup_form(request: Request, error: str = ""):
    if auth.mode() == "none" or not auth.needs_setup():
        return RedirectResponse("/", status_code=303)
    return page(request, "signin.html", setup=True, error=error, nav="")


@app.post("/setup")
def setup_save(username: str = Form(""), password: str = Form("")):
    if auth.mode() == "none" or not auth.needs_setup():
        return RedirectResponse("/", status_code=303)
    try:
        uid = auth.create_user(username, password, role="admin")
    except ValueError as e:
        return RedirectResponse(f"/setup?error={urllib.parse.quote(str(e))}",
                                status_code=303)
    return _set_session(RedirectResponse("/", status_code=303), auth.create_session(uid))


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, error: str = ""):
    if auth.mode() == "none":
        return RedirectResponse("/", status_code=303)
    if auth.needs_setup():
        return RedirectResponse("/setup", status_code=303)
    if current_user(request):
        return RedirectResponse("/", status_code=303)
    return page(request, "signin.html", setup=False, error=error, nav="")


@app.post("/login")
def login_save(username: str = Form(""), password: str = Form("")):
    user = auth.verify(username, password)
    if not user:
        return RedirectResponse(
            "/login?error=" + urllib.parse.quote("that username and password do not match"),
            status_code=303)
    return _set_session(RedirectResponse("/", status_code=303),
                        auth.create_session(user["id"]))


@app.post("/logout")
def logout(request: Request):
    auth.delete_session(request.cookies.get(auth.SESSION_COOKIE))
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.SESSION_COOKIE, path="/")
    return resp


@app.post("/api/theme")
def api_theme(request: Request, theme: str = Form("dark")):
    """Remember the theme against the account, when there is one.

    With sign-in off the browser keeps it in local storage instead, which is
    the only place it can live when nobody is identified.
    """
    theme = theme if theme in ("light", "dark") else "dark"
    user = current_user(request)
    if user:
        auth.set_pref(user["id"], "theme", theme)
        return JSONResponse({"ok": True, "stored": "account"})
    return JSONResponse({"ok": True, "stored": "browser"})


# ---------- lifecycle ----------

@app.on_event("startup")
async def startup():
    db.init()
    # A test drives sync itself and asserts on the result. A loop waking up
    # underneath it rewrites the database mid-assertion, which is a flake that
    # takes an afternoon to explain.
    if os.environ.get("COUCHELEPHANT_NO_SYNC_LOOP") == "1":
        return
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
    # Keyed by broadcast, not by title, or every episode of a daily programme
    # showed as being recorded because one of them is.
    plex_slots = {(r["channel_vcn"], r["begins_at"]) for r in db.query(
        "SELECT channel_vcn, begins_at FROM plex_grabs "
        "WHERE status IN ('scheduled','inprogress')")}

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
            elif (r["channel_vcn"], r["begins_at"]) in plex_slots:
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


def _why_for(airing):
    """Why this broadcast is set to record, in words, or nothing."""
    r = db.one(
        """SELECT o.source, o.pass_id, p.kind, p.team_name, p.series_title
           FROM our_grabs o LEFT JOIN passes p ON p.id = o.pass_id
           WHERE o.airing_id = ?""", (airing["id"],))
    if r:
        if r["pass_id"] and (r["team_name"] or r["series_title"]):
            name = r["team_name"] or r["series_title"]
            return {"who": "ce",
                    "kind": "sports" if r["kind"] == "team" else "series",
                    "text": f"CouchElephant booked this for the {name} pass.",
                    "pass_id": r["pass_id"]}
        return {"who": "ce", "kind": "one",
                "text": "You recorded this one broadcast by hand.", "pass_id": None}
    hit = db.one("SELECT subscription, status FROM plex_grabs WHERE channel_vcn = ? "
                 "AND begins_at = ? LIMIT 1", (airing["channel_vcn"], airing["begins_at"]))
    if hit:
        sub = db.one("SELECT title FROM plex_subscriptions WHERE key = ?",
                     (hit["subscription"] or "",))
        title = sub["title"] if sub else None
        if title and not title.startswith("This "):
            return {"who": "plex", "kind": "series",
                    "text": f"Plex is recording this under its own rule, {title}.",
                    "pass_id": None}
        return {"who": "plex", "kind": "one",
                "text": "This was scheduled in Plex, not here.", "pass_id": None}
    return None


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
    # This broadcast, not any programme sharing its title. A daily show keeps
    # the same title, so a title match reported every future airing as covered.
    scheduled = db.one(
        "SELECT status FROM plex_grabs WHERE channel_vcn = ? AND begins_at = ? "
        "AND status IN ('scheduled','inprogress','complete') LIMIT 1",
        (a["channel_vcn"], a["begins_at"]))
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
        "why": _why_for(a),
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
                "value": value, "options": _enum(st.get("enumValues")),
            })
        out.append({
            "index": i, "title": title, "type": s_.get("type"),
            "one_shot": one_shot, "settings": settings,
        })
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


# A pass always pins each booking to the airing it chose, so these three are
# not its to carry. Storing them is noise at best, and a reader could believe
# a pass had been limited to one channel when it had not.
_PASS_PREF_BLOCKED = frozenset(("oneShot", "lineupChannel", "startTimeslot"))


def _pass_prefs(prefs):
    return {k: v for k, v in (prefs or {}).items() if k not in _PASS_PREF_BLOCKED}


def _make_pass(kind, team=None, series=None, nets=None, chans=None, prefs=None,
               update=True):
    """Create or update one CouchElephant pass.

    The only place a pass is written. There were three, and they had already
    drifted: one checked for a duplicate, one checked the wrong column, one did
    not check at all.

    Returns (pass_id, label, created).
    """
    nets, chans, prefs = nets or [], chans or [], _pass_prefs(prefs)
    if kind == "team":
        existing = db.one("SELECT id FROM passes WHERE kind='team' AND team_id = ?",
                          (team["id"],))
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
        if kind == "team":
            c.execute("INSERT INTO passes (kind, team_id, team_name, networks, channels, "
                      "prefs, enabled, created_at) VALUES ('team',?,?,?,?,?,1,?)",
                      (team["id"], team["name"], db.js(nets), db.js(chans),
                       db.js(prefs), int(time.time())))
        else:
            c.execute("INSERT INTO passes (kind, series_title, series_guid, networks, "
                      "channels, prefs, enabled, created_at) "
                      "VALUES ('series',?,?,?,?,?,1,?)",
                      (series, series, db.js(nets), db.js(chans), db.js(prefs),
                       int(time.time())))
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

        prefs = dict(db.unjs(settings, {}) or {})
        prefs = {k: v for k, v in prefs.items() if v is not None}

        if (nets or chans) and not one_shot:
            # The settings the user just filled in belong to the pass too, or
            # padding and quality are silently lost by choosing a source limit.
            return await asyncio.to_thread(_make_ce_rule, row, chosen, nets, chans,
                                           prefs)

        # oneShot is never shown, and it is what separates one game from every
        # future airing of the same programme. It follows the template.
        prefs["oneShot"] = "1" if one_shot else "0"

        await asyncio.to_thread(passes._schedule, plex, row, None, "manual", chosen, prefs)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=500)
    await asyncio.to_thread(sync.sync_recordings, plex)
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
        "SELECT status FROM plex_grabs WHERE channel_vcn = ? AND begins_at = ? "
        "AND status IN ('inprogress','complete') LIMIT 1",
        (mine["channel_vcn"], mine["begins_at"]))
    if started:
        return JSONResponse({"ok": True, "message":
                             "Cancelled. It had already started, so what Plex "
                             "captured so far is still in your library."})
    return JSONResponse({"ok": True, "message": "Recording cancelled."})


@app.post("/api/pass")
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
                channel=channel, sports=sports,
                q=q, logos=_logo_map(), f=f, x=x,
                nfilters=len(filters.parse(f)) + len(filters.parse(x)))


@app.get("/search")
def search_redirect(q: str = ""):
    """Search used to be its own tab. Keep the URL working."""
    return RedirectResponse(f"/?q={q}" if q else "/", status_code=307)


# ---------- recordings ----------

@app.get("/recordings", response_class=HTMLResponse)
def recordings(request: Request):
    """The shell. Everything on it is fetched from /api/schedule and /api/rules.

    This route used to build the rules, the grabs, the upcoming picks, the
    teams and the decision log on every page load, for a template that stopped
    reading any of them. The upcoming picks alone ran a full pass evaluation
    and threw it away.
    """
    return page(request, "recordings.html")


# ---------- passes ----------

@app.get("/passes")
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
                      p.kind, p.team_name, p.series_title, p.enabled
               FROM our_grabs o LEFT JOIN passes p ON p.id = o.pass_id"""):
        if r["pass_id"] and (r["team_name"] or r["series_title"]):
            out[(r["channel_vcn"], r["begins_at"])] = {
                "who": "ce",
                "kind": "sports" if r["kind"] == "team" else "series",
                "reason": f"the {r['team_name'] or r['series_title']} pass",
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


@app.get("/api/schedule")
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


@app.get("/api/series")
def api_series(q: str = ""):
    """Programmes in the guide that a rule could follow.

    Grouped by the show rather than the episode, because a rule follows the
    series. Only what is actually coming up, so the list cannot offer something
    the guide can no longer record.
    """
    q = (q or "").strip()
    like = f"%{q}%"
    rows = db.query(
        """SELECT COALESCE(NULLIF(p.grandparent_title,''), p.title) AS name,
                  COUNT(DISTINCT a.id) AS airings,
                  MIN(a.begins_at) AS next_at
           FROM airings a JOIN programs p ON p.guid = a.program_guid
           WHERE a.begins_at > ? AND (? = '' OR name LIKE ? COLLATE NOCASE)
           GROUP BY name ORDER BY airings DESC, name LIMIT 40""",
        (int(time.time()), q, like))
    return JSONResponse({"ok": True, "series": [dict(r) for r in rows]})


@app.get("/api/teams")
def api_teams(q: str = ""):
    q = (q or "").strip()
    # No low cap here. Teams only exist while the guide carries a game they
    # play, so the whole list is short, and silently showing 60 of 82 would
    # look like a team had gone missing.
    rows = db.query(
        "SELECT t.id, t.name, EXISTS(SELECT 1 FROM passes p WHERE p.team_id = t.id) "
        "AS followed FROM teams t WHERE ? = '' OR t.name LIKE ? COLLATE NOCASE "
        "ORDER BY t.name LIMIT 400", (q, f"%{q}%"))
    total = db.one("SELECT COUNT(*) c FROM teams")["c"]
    return JSONResponse({"ok": True, "total": total,
                         "teams": [dict(r) for r in rows]})


@app.get("/api/sources")
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


@app.get("/api/rules/options")
def api_rule_options(kind: str = "team", team_id: str = "", series: str = ""):
    """Plex's own options for a rule that follows this team or programme.

    A template belongs to a programme, so one upcoming broadcast stands in for
    the rest. Only the recurring choices are offered here: a rule that records
    one broadcast is not a rule.
    """
    if kind == "team":
        rows = passes.candidate_airings(int(team_id or 0))
    else:
        rows = passes.series_airings((series or "").strip())
    if not rows:
        return JSONResponse({"ok": False, "error":
                             "Nothing from this is in the guide yet, so Plex has no "
                             "options to offer for it."})
    row = rows[0]
    try:
        plex = Plex(db.get_setting("plex_url"), db.get_setting("plex_token"))
        options = passes.templates(plex, row)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"})

    payload = [t for t in _template_payload(options, row, pin=False) if not t["one_shot"]]
    # Put the one that names what you chose first, since that is the rule the
    # user means; Plex lists them in its own order.
    if kind == "team":
        t = db.one("SELECT name FROM teams WHERE id = ?", (team_id or 0,))
        want = t["name"] if t else ""
    else:
        want = (series or "").strip()
    if want:
        payload.sort(key=lambda t: 0 if want.lower() in (t["title"] or "").lower() else 1)
    return JSONResponse({"ok": True, "templates": payload,
                         "sample": row["title"]})


@app.get("/api/rules")
def api_rules():
    """Every pass, with what it follows and where it may record from."""
    out = []
    for p in db.query("SELECT * FROM passes ORDER BY COALESCE(team_name, series_title)"):
        nets = db.unjs(p["networks"]) or []
        chans = db.unjs(p["channels"]) or []
        booked = db.one("SELECT COUNT(*) c FROM our_grabs WHERE pass_id = ?", (p["id"],))["c"]
        out.append({
            "id": p["id"], "who": "ce", "kind": p["kind"],
            "icon": "sports" if p["kind"] == "team" else "series",
            "title": p["team_name"] or p["series_title"],
            "team_id": p["team_id"], "series": p["series_title"],
            "networks": nets, "channels": chans,
            "prefs": db.unjs(p["prefs"], {}) or {},
            "enabled": bool(p["enabled"]), "booked": booked,
            "detail": ("Every game, from the live broadcast" if p["kind"] == "team"
                       else "Every episode the guide carries"),
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


@app.get("/api/rules/{rule_id}/upcoming")
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


@app.post("/api/rules/{rule_id}")
async def api_rule_edit(rule_id: int, networks: str = Form(""),
                        channels: str = Form(""), enabled: str = Form(""),
                        settings: str = Form("")):
    """Change a pass: where it may record from, its Plex settings, or pause it."""
    r = db.one("SELECT * FROM passes WHERE id = ?", (rule_id,))
    if not r:
        return JSONResponse({"ok": False, "error": "no such pass"}, status_code=404)
    nets = db.unjs(networks, []) or []
    chans = db.unjs(channels, []) or []
    # An empty settings field means "not sent", not "clear them".
    prefs = db.unjs(settings, None) if settings else None
    keep = db.js(_pass_prefs(prefs)) if prefs is not None else r["prefs"]
    with db.tx() as c:
        c.execute("UPDATE passes SET networks=?, channels=?, prefs=?, enabled=? WHERE id=?",
                  (db.js(nets), db.js(chans), keep,
                   1 if enabled not in ("0", "false") else 0, rule_id))
    done = await asyncio.to_thread(passes.run_passes)
    made = len([d for d in done if d["action"] == "scheduled"])
    where = " or ".join(nets + chans)
    return JSONResponse({"ok": True, "message":
                         (f"Saved. Only from {where}. " if where else "Saved. ")
                         + f"{made} new airing(s) scheduled."})


@app.post("/api/rules")
async def api_rule_create(kind: str = Form("team"), team_id: str = Form(""),
                          series: str = Form(""), networks: str = Form(""),
                          channels: str = Form(""), template: int = Form(0),
                          settings: str = Form("")):
    """Create a schedule that keeps matching new airings.

    With no source limit this is something Plex can express on its own, so it
    becomes a plain Plex rule and Plex takes it from there. Name more than one
    network or channel and Plex cannot say it, so CouchElephant keeps the rule
    and books each airing itself.
    """
    nets = db.unjs(networks, []) or []
    chans = db.unjs(channels, []) or []
    prefs = dict(db.unjs(settings, {}) or {})

    if kind == "team":
        t = db.one("SELECT * FROM teams WHERE id = ?", (team_id or 0,))
        if not t:
            return JSONResponse({"ok": False, "error": "pick a team"}, status_code=400)
        label, rows = t["name"], passes.candidate_airings(t["id"])
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


def _make_plex_rule(label, rows, template, prefs):
    """Hand the rule to Plex, which is all it needs when nothing narrows it."""
    if not rows:
        return JSONResponse({"ok": False, "error":
                             f"Nothing from {label} is in the guide yet, so there is "
                             "nothing for Plex to make a rule from."}, status_code=400)
    row = rows[0]
    try:
        plex = Plex(db.get_setting("plex_url"), db.get_setting("plex_token"))
        options = [t for t in passes.templates(plex, row)
                   if not (t.get("title") or "").lower().startswith("this ")]
        if not options:
            raise PlexError("Plex offered no recurring rule for this")
        chosen = options[template] if 0 <= template < len(options) else options[0]
        prefs = dict(prefs)
        prefs["oneShot"] = "0"
        key = plex.create_recording(
            chosen["parameters"],
            chosen.get("targetLibrarySectionID") or 2,
            int(chosen.get("type") or 2), prefs)
        # `is False` on purpose: None means the check failed, not that the
        # rule is gone.
        if key and plex.subscription_exists(key) is False:
            raise PlexError("Plex accepted the rule and then discarded it. It may "
                            "already have one for this.")
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=500)
    sync.sync_recordings(plex)
    return JSONResponse({"ok": True, "ce_rule": False, "message":
                         f"Plex is now recording {chosen.get('title') or label}. "
                         "It appears in the schedule as Plex's own rule."})


@app.post("/api/plexrule/{key}/delete")
def api_plexrule_delete(key: str):
    """Remove one of Plex's own recurring rules."""
    try:
        plex = Plex(db.get_setting("plex_url"), db.get_setting("plex_token"))
        plex.delete_subscription(key)
        sync.sync_recordings(plex)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"}, status_code=500)
    return JSONResponse({"ok": True, "message": "Removed from Plex."})


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

def _channel_rows():
    """Every channel, and where its logo comes from."""
    out = []
    for c in db.query("SELECT * FROM channels ORDER BY CAST(vcn AS REAL), vcn"):
        custom = bool(c["custom_logo"] and os.path.exists(c["custom_logo"]))
        theirs = bool(c["logo_path"] and os.path.exists(c["logo_path"]))
        out.append({
            "vcn": c["vcn"], "call_sign": c["call_sign"] or "",
            "network": c["network"] or "",
            "custom": custom, "has_logo": custom or theirs,
            "source": "yours" if custom else ("guide" if theirs else "none"),
            "v": c["custom_logo_at"] or c["logo_fetched_at"] or 0,
        })
    return out


@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, tested: str = "", autherror: str = ""):
    return page(request, "settings.html", zones=ZONES, tested=tested,
                autherror=autherror, logos=sync.logo_coverage(),
                channels=_channel_rows(), users=auth.list_users(),
                cf_ready=cf_access.available())


@app.get("/partial/settings", response_class=HTMLResponse)
def settings_partial(request: Request, tested: str = "", autherror: str = ""):
    """The settings window on its own, for the overlay the gear opens."""
    return page(request, "_settings.html", zones=ZONES, tested=tested,
                autherror=autherror, logos=sync.logo_coverage(),
                channels=_channel_rows(), users=auth.list_users(),
                cf_ready=cf_access.available())


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


@app.post("/settings/auth")
def settings_auth(request: Request, auth_mode: str = Form("none"),
                  cf_team_domain: str = Form(""), cf_aud: str = Form("")):
    """Change how, or whether, people sign in.

    Turning sign-in on with no accounts sends the next visitor to create one.
    Turning it off is deliberately allowed from inside: someone locked out of
    Cloudflare Access can still reach the box on the LAN and switch back.
    """
    mode = auth_mode if auth_mode in auth.MODES else "none"
    if mode == "cloudflare":
        ok, detail = cf_access.check(cf_team_domain.strip(), cf_aud.strip())
        if not ok:
            return RedirectResponse(
                "/settings?autherror=" + urllib.parse.quote(detail), status_code=303)
    db.set_setting("cf_team_domain", cf_team_domain.strip())
    db.set_setting("cf_aud", cf_aud.strip())
    db.set_setting("auth_mode", mode)
    return RedirectResponse("/settings", status_code=303)


@app.post("/settings/users/{uid}/delete")
def settings_user_delete(request: Request, uid: int):
    me = current_user(request)
    if me and me["id"] == uid:
        return RedirectResponse(
            "/settings?autherror=" + urllib.parse.quote("you cannot delete your own account"),
            status_code=303)
    auth.delete_user(uid)
    return RedirectResponse("/settings", status_code=303)


def _test_plex(url=None, token=None):
    """Check a server, and say something useful when it does not answer.

    Each step is separate so the reason names the step that failed. A single
    try around the lot can only ever report the last exception, which is how a
    bad token comes to read as "server unreachable".

    Candidate values can be passed in. First run does that rather than saving
    them, testing, and reverting, which left a window for the sync loop to run
    against half-entered credentials.
    """
    url = url if url is not None else db.get_setting("plex_url")
    token = token if token is not None else db.get_setting("plex_token")
    if not url:
        return False, "No server address set. Fill in the address above and save."
    if not token:
        return False, "No token set. Paste the token above and save."

    plex = Plex(url, token)
    try:
        info = plex.server_info()
    except PlexError as e:
        if getattr(e, "status", None) in (401, 403):
            return False, ("The server answered but rejected the token. Check the "
                           "PlexOnlineToken in Preferences.xml on the server.")
        return False, f"Could not reach {url}. {e}"
    except Exception as e:
        # Almost always a wrong host, a wrong port, or nothing listening.
        return False, (f"Could not reach {url}. {type(e).__name__}: {e}. "
                       "The address has to work from inside this container, so "
                       "127.0.0.1 only works if Plex runs in it too.")

    name = info.get("friendlyName") or "the server"
    version = info.get("version") or "unknown version"
    try:
        dvrs = plex.dvrs()
    except Exception as e:
        return False, (f"Reached {name} (Plex {version}), but could not read its "
                       f"DVRs: {type(e).__name__}: {e}")
    if not dvrs:
        return False, (f"Reached {name} (Plex {version}), but it has no DVR. "
                       "Add a tuner to Plex before CouchElephant can record.")

    lineup = dvrs[0].get("lineupTitle") or "a lineup"
    return True, (f"{name}, Plex {version}. "
                  f"{len(dvrs)} DVR{'' if len(dvrs) == 1 else 's'}, {lineup}.")


@app.post("/settings/test")
def settings_test(request: Request):
    ok, detail = _test_plex()
    if "application/json" in (request.headers.get("accept") or ""):
        return JSONResponse({"ok": ok, "detail": detail})
    prefix = "OK: " if ok else "FAILED: "
    return RedirectResponse("/settings?tested=" + urllib.parse.quote(prefix + detail),
                            status_code=303)


# What a browser will actually render, by the file's own first bytes rather
# than by the name it arrived under.
_IMAGE_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"\xff\xd8\xff", "jpg"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
)
_MAX_LOGO = 2 * 1024 * 1024


def _sniff_image(blob):
    for magic, kind in _IMAGE_MAGIC:
        if blob.startswith(magic):
            return kind
    if blob[:4] == b"RIFF" and blob[8:12] == b"WEBP":
        return "webp"
    return None


def _is_svg(blob):
    """SVG is refused, not accepted and then broken.

    It cannot be served as image/png, and serving an uploaded document as
    image/svg+xml runs any script inside it on this origin.
    """
    head = blob.lstrip()[:200].lower()
    return head.startswith(b"<?xml") or head.startswith(b"<svg") or b"<svg" in head


@app.post("/settings/channels/{vcn}/logo")
async def channel_logo_upload(vcn: str, logo: UploadFile = File(...)):
    """Use a logo of your own for this channel."""
    row = db.one("SELECT vcn, custom_logo FROM channels WHERE vcn = ?", (vcn,))
    if not row:
        return JSONResponse({"ok": False, "error": "unknown channel"}, status_code=404)
    blob = await logo.read()
    if not blob:
        return JSONResponse({"ok": False, "error": "that file is empty"}, status_code=400)
    if len(blob) > _MAX_LOGO:
        return JSONResponse({"ok": False, "error":
                             f"that file is {len(blob)//1024} KB. The limit is "
                             f"{_MAX_LOGO//1024} KB."}, status_code=400)
    # Trust the bytes, not the extension. A file named .png that is not one
    # would render as a broken image on every page that shows the channel.
    kind = _sniff_image(blob)
    if not kind:
        if _is_svg(blob):
            return JSONResponse({"ok": False, "error":
                                 "SVG cannot be used here. Save it as a PNG and "
                                 "upload that."}, status_code=400)
        return JSONResponse({"ok": False, "error":
                             "that is not an image file (PNG, JPEG, GIF or WebP)."},
                            status_code=400)

    os.makedirs(sync.LOGO_DIR, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in vcn)
    path = os.path.join(sync.LOGO_DIR, f"custom-{safe}.{kind}")
    tmp = path + ".part"
    with open(tmp, "wb") as fh:
        fh.write(blob)
    os.replace(tmp, path)
    # A different extension than last time leaves the old file behind.
    old = row["custom_logo"]
    if old and old != path and os.path.exists(old):
        try:
            os.remove(old)
        except OSError:
            pass
    with db.tx() as c:
        c.execute("UPDATE channels SET custom_logo = ?, custom_logo_at = ? WHERE vcn = ?",
                  (path, int(time.time()), vcn))
    return JSONResponse({"ok": True, "message": f"Logo set for {vcn}.",
                         "v": int(time.time())})


@app.post("/settings/channels/{vcn}/logo/reset")
def channel_logo_reset(vcn: str):
    """Go back to whatever the guide provides for this channel."""
    row = db.one("SELECT custom_logo FROM channels WHERE vcn = ?", (vcn,))
    if not row:
        return JSONResponse({"ok": False, "error": "unknown channel"}, status_code=404)
    if row["custom_logo"] and os.path.exists(row["custom_logo"]):
        try:
            os.remove(row["custom_logo"])
        except OSError:
            pass
    with db.tx() as c:
        c.execute("UPDATE channels SET custom_logo = NULL, custom_logo_at = NULL "
                  "WHERE vcn = ?", (vcn,))
    has = db.one("SELECT logo_path FROM channels WHERE vcn = ?", (vcn,))
    back = bool(has and has["logo_path"] and os.path.exists(has["logo_path"]))
    return JSONResponse({"ok": True, "v": int(time.time()),
                         "message": (f"{vcn} is back to the guide's logo." if back
                                     else f"{vcn} has no logo again. The guide offers none.")})


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


_LOGO_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".gif": "image/gif", ".webp": "image/webp"}


@app.get("/logo/{vcn}")
def logo(vcn: str, v: str = ""):
    """Channel logo. A logo you supplied wins over the guide's own."""
    row = db.one("SELECT logo_path, custom_logo FROM channels WHERE vcn = ?", (vcn,))
    path = None
    if row:
        path = row["custom_logo"] if (row["custom_logo"] and
                                      os.path.exists(row["custom_logo"])) else row["logo_path"]
    if path and os.path.exists(path):
        # An uploaded JPEG served as image/png renders as nothing in some
        # browsers, so the type follows the file.
        kind = _LOGO_TYPES.get(os.path.splitext(path)[1].lower(), "image/png")
        return FileResponse(path, media_type=kind,
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
