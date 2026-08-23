"""FastAPI app: middleware, lifecycle, and the route modules in `routes/`."""
import asyncio
import os
import time
import urllib.parse

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import auth, backingstore, backups, db, passes, sync
from .routes import account, data, guide, record, settings
from .routes import passes as pass_routes
from .routes._shared import ASSET_V, BASE, VERSION, current_user  # noqa: F401
from .routes.account import _configured

app = FastAPI(title="CouchElephant", docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")

_OPEN_EXACT = frozenset(("/login", "/setup", "/logout", "/healthz", "/welcome",
                         "/favicon.ico"))
_OPEN_PREFIX = ("/static/",)


# What a signed-in non-administrator may not change: the Plex connection,
# accounts, artwork, and anything that copies or replaces the database. They
# keep the guide, the schedule and the passes, which is what the app is for.
_ADMIN_PREFIX = ("/settings", "/api/backups", "/api/backingstore", "/api/import",
                 "/api/export")


def _admin_only(path, method):
    if path.startswith("/api/theme"):
        return False
    if path in ("/settings", "/partial/settings") and method == "GET":
        return False          # reading settings is fine; changing them is not
    return path.startswith(_ADMIN_PREFIX)


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
    user = current_user(request)
    if user:
        if _admin_only(path, request.method) and user.get("role") != "admin":
            if path.startswith("/api/"):
                return JSONResponse({"ok": False, "error": "administrators only"},
                                    status_code=403)
            return HTMLResponse("Administrators only.", status_code=403)
        return await call_next(request)
    # A fresh switch to local sign-in has no accounts yet, so send the first
    # visitor to create one rather than to a login they cannot pass.
    where = "/setup" if auth.needs_setup() else "/login"
    if path.startswith("/api/") or path.startswith("/partial/"):
        return JSONResponse({"ok": False, "error": "sign in required"}, status_code=401)
    return RedirectResponse(where, status_code=303)



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


for r in (account, data, guide, record, pass_routes, settings):
    app.include_router(r.router)
