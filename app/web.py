"""FastAPI app: guide, search, recordings, passes, settings."""
import asyncio
import datetime
import os
import time
import urllib.parse
import uuid
import zoneinfo

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import (auth, backingstore, backups, cf_access, db, dbstore, filters,
               passes, portable, smartfilter, sync, teamcat)
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


# ---------- database: export, import, snapshots, backing store ----------

@app.get("/api/export")
def api_export(secrets: int = 0):
    """Download everything you decided, in one file."""
    blob = portable.export_bytes(include_secrets=bool(secrets), version=VERSION)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return Response(
        blob, media_type="application/zip",
        headers={"Content-Disposition":
                 f'attachment; filename="couchelephant-{stamp}.zip"'})


@app.post("/api/import/inspect")
async def api_import_inspect(file: UploadFile = File(...)):
    """What is in this file, before anything is written."""
    try:
        return JSONResponse({"ok": True, **portable.describe(await file.read())})
    except portable.ImportError_ as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/api/import")
async def api_import(file: UploadFile = File(...), replace: str = Form(""),
                     secrets: str = Form("")):
    """Read an export back in."""
    blob = await file.read()
    try:
        report = await asyncio.to_thread(
            portable.import_bytes, blob,
            str(replace).lower() in ("1", "true", "yes"),
            str(secrets).lower() in ("1", "true", "yes"))
    except portable.ImportError_ as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    total = sum(v["written"] for v in report["stores"].values())
    removed = sum(v["deleted"] for v in report["stores"].values())
    report["message"] = (f"{total} record(s) imported"
                         + (f", {removed} removed" if removed else "")
                         + (f", {report['logos']} logo(s)" if report["logos"] else "")
                         + ". The guide refreshes on the next sync.")
    return JSONResponse(report)


@app.get("/api/backups/jobs")
def api_backup_jobs():
    return JSONResponse({"ok": True, "jobs": backups.jobs()})


@app.post("/api/backups/jobs")
def api_backup_job_save(job_id: str = Form(""), name: str = Form("Backup"),
                        dest_path: str = Form(""), every_hours: str = Form("24"),
                        retention: str = Form("7"), passphrase: str = Form(""),
                        enabled: str = Form("1"), raw_db: str = Form("1"),
                        with_secrets: str = Form("")):
    def on(v):
        return str(v).lower() in ("1", "true", "yes", "on")
    try:
        jid = backups.save_job(
            int(job_id) if job_id else None, name=name, dest_path=dest_path,
            every_hours=every_hours, retention=retention, passphrase=passphrase,
            enabled=on(enabled), raw_db=on(raw_db), with_secrets=on(with_secrets))
    except (TypeError, ValueError) as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return JSONResponse({"ok": True, "id": jid, "jobs": backups.jobs()})


@app.post("/api/backups/jobs/{job_id}/delete")
def api_backup_job_delete(job_id: int):
    backups.delete_job(job_id)
    return JSONResponse({"ok": True, "jobs": backups.jobs()})


@app.post("/api/backups/jobs/{job_id}/run")
async def api_backup_job_run(job_id: int):
    try:
        out = await asyncio.to_thread(backups.run_job, job_id, VERSION)
    except FileNotFoundError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=404)
    out["jobs"] = backups.jobs()
    return JSONResponse(out, status_code=200 if out.get("ok") else 400)


@app.get("/api/backups/archives")
def api_backup_archives(dest: str = ""):
    return JSONResponse({"ok": True, "archives": backups.archives(dest)})


@app.post("/api/backups/restore")
async def api_backup_restore(dest: str = Form(...), name: str = Form(...),
                             passphrase: str = Form(""), replace: str = Form("1")):
    try:
        report = await asyncio.to_thread(
            backups.restore, dest, name, passphrase,
            str(replace).lower() in ("1", "true", "yes"), VERSION)
    except portable.ImportError_ as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"},
                            status_code=400)
    return JSONResponse(report)


@app.get("/api/backingstore/config")
def api_backingstore_config():
    cfg = backingstore.config()
    # A stored password is never handed back, only whether there is one.
    shown = {k: ("*" * 8 if v else "") if k in backingstore.SECRET_KEYS else v
             for k, v in cfg.items()}
    return JSONResponse({
        "ok": True, "config": shown,
        "backends": [{"name": b.name, "label": b.label,
                      "fields": [{"key": f[0], "label": f[1], "kind": f[2]}
                                 for f in b.fields]}
                     for b in backingstore.BACKENDS.values()],
        "stores": [{"name": n, "label": s["label"]}
                   for n, s in dbstore.STORES.items()],
        "status": backingstore.status(),
    })


@app.post("/api/backingstore/config")
async def api_backingstore_config_save(request: Request):
    form = await request.form()
    for key in backingstore.CONFIG_KEYS:
        if key not in form:
            continue
        value = (form.get(key) or "").strip()
        # A masked password means "leave it alone", not "set it to asterisks".
        if key in backingstore.SECRET_KEYS and set(value) == {"*"}:
            continue
        db.set_setting(key, value)
    return JSONResponse({"ok": True})


@app.post("/api/backingstore/test")
async def api_backingstore_test():
    backend = backingstore.chosen()
    if backend is None:
        return JSONResponse({"ok": False, "error": "Pick a backing store first."},
                            status_code=400)
    try:
        detail = await asyncio.to_thread(backend.test)
    except backingstore.BackendError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"},
                            status_code=400)
    return JSONResponse({"ok": True, "detail": detail})


@app.post("/api/backingstore/run")
async def api_backingstore_run(dry_run: str = Form("")):
    try:
        out = await asyncio.to_thread(
            backingstore.sync_all, str(dry_run).lower() in ("1", "true", "yes"))
    except backingstore.BackendError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return JSONResponse(out)


@app.post("/api/backingstore/restore")
async def api_backingstore_restore(dry_run: str = Form("")):
    try:
        out = await asyncio.to_thread(
            backingstore.restore_from_remote,
            str(dry_run).lower() in ("1", "true", "yes"))
    except backingstore.BackendError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return JSONResponse(out)


@app.get("/api/backingstore/status")
def api_backingstore_status():
    return JSONResponse({"ok": True, "status": backingstore.status(),
                         "configured": backingstore.chosen() is not None})


# ---------- lifecycle ----------

@app.on_event("startup")
async def startup():
    db.init()
    # A test drives sync itself and asserts on the result. A loop waking up
    # underneath it rewrites the database mid-assertion, which is a flake that
    # takes an afternoon to explain.
    backups.init()
    if os.environ.get("COUCHELEPHANT_NO_SYNC_LOOP") == "1":
        return
    asyncio.create_task(sync_loop())
    asyncio.create_task(backup_loop())
    asyncio.create_task(backingstore_loop())


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


async def backup_loop():
    """Run each backup job when its own interval says so."""
    await asyncio.sleep(20)
    while True:
        try:
            now = int(time.time())
            for job in await asyncio.to_thread(backups.jobs):
                if not job["enabled"] or not job["every_hours"]:
                    continue
                due = (job["last_run"] or 0) + job["every_hours"] * 3600
                if now >= due:
                    await asyncio.to_thread(backups.run_job, job["id"], VERSION)
        except Exception:
            # A backup that fails is recorded on the job. The loop must not
            # be the thing that stops.
            pass
        await asyncio.sleep(300)


async def backingstore_loop():
    """Reconcile with the backing store on its own timer.

    It also runs shortly after startup, so a machine that was off picks up
    what changed elsewhere without anybody pressing anything.
    """
    await asyncio.sleep(45)
    while True:
        try:
            minutes = int(db.get_setting("backingstore_auto_minutes") or 0)
        except ValueError:
            minutes = 0
        if minutes and backingstore.chosen() is not None:
            try:
                await asyncio.to_thread(backingstore.sync_all)
            except Exception as e:
                backingstore._status(ok=False, at=int(time.time()),
                                     detail=f"{type(e).__name__}: {e}")
        await asyncio.sleep(max(5, minutes or 30) * 60)


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


# One icon per kind of pass, named in one place so the guide panel, the
# schedule and the pass list cannot drift apart.
_PASS_ICON = {"team": "sports", "series": "series", "smart": "smart"}


def _why_for(airing):
    """Why this broadcast is set to record, in words, or nothing."""
    r = db.one(
        """SELECT o.source, o.pass_id, p.kind, p.team_name, p.series_title, p.label
           FROM our_grabs o LEFT JOIN passes p ON p.id = o.pass_id
           WHERE o.airing_id = ?""", (airing["id"],))
    if r:
        name = r["team_name"] or r["series_title"] or r["label"]
        if r["pass_id"] and name:
            return {"who": "ce", "kind": _PASS_ICON.get(r["kind"], "series"),
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


TEAM_PAGE = 300


@app.get("/api/teams")
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
SPORTS_PADDING = {"startOffsetMinutes": "1", "endOffsetMinutes": "60"}


@app.get("/api/rules/options")
def api_rule_options(kind: str = "team", team_id: str = "", series: str = "",
                     filter: str = "", ce_pass: int = 0):
    """Plex's own options for a rule that follows this team, programme or filter.

    A template belongs to a programme, so one upcoming broadcast stands in for
    the rest.

    `ce_pass` asks for the settings a CouchElephant pass will actually use,
    which are the one-shot template's, because that is what it books for each
    airing. Without it the recurring choices are offered, which is right when
    the rule is about to become Plex's own.
    """
    if kind == "smart":
        tree = db.unjs(filter, None)
        if not tree:
            return JSONResponse({"ok": False, "error": "add a condition first"})
        try:
            rows = passes.smart_airings(tree)
        except smartfilter.FilterError as e:
            return JSONResponse({"ok": False, "error": str(e)})
    elif kind == "team":
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
    sporty = kind == "team" or (row["section"] if "section" in row.keys() else "") == "sports"
    return JSONResponse({"ok": True, "templates": payload, "sample": row["title"],
                         "sporty": bool(sporty),
                         "sports_padding": SPORTS_PADDING})


@app.get("/api/rules")
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

    with db.tx() as c:
        c.execute("UPDATE passes SET networks=?, channels=?, prefs=?, filter=?, "
                  "label=?, enabled=? WHERE id=?",
                  (db.js(nets), db.js(chans), keep, filt, label,
                   1 if enabled not in ("0", "false") else 0, rule_id))
    done = await asyncio.to_thread(passes.run_passes)
    made = len([d for d in done if d["action"] == "scheduled"])
    where = " or ".join(nets + chans)
    return JSONResponse({"ok": True, "message":
                         (f"Saved. Only from {where}. " if where else "Saved. ")
                         + f"{made} new airing(s) scheduled."})


SMART_LIMIT = 40          # airings a smart filter may book without being told twice


@app.get("/api/filter/fields")
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
        "kinds": [{"value": v, "label": l} for v, l in smartfilter.KINDS],
        "weekdays": [{"value": v, "label": l} for v, l in smartfilter.WEEKDAYS],
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
        "comparisons": {k: [{"value": c, "label": l} for c, l in v]
                        for k, v in smartfilter.COMPARISONS.items()},
        "values": values,
        "coverage": {"programs": total, "rated": have},
        "limit": SMART_LIMIT,
    })


@app.post("/api/filter/preview")
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
    for guid, airings in list(games.items()):
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


@app.post("/api/rules")
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
