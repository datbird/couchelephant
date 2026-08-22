"""FastAPI app: guide, search, recordings, passes, settings."""
import asyncio
import datetime
import os
import time
import zoneinfo

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import db, passes, sync
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

@app.get("/", response_class=HTMLResponse)
def guide(request: Request, day: str = "", channel: str = "", sports: int = 0, q: str = ""):
    """Guide and search are the same page.

    A query switches the view from one day's grid to matches across the whole
    guide, because they answer the same question and splitting them into two
    tabs made you navigate to find out what is on.
    """
    now = int(time.time())
    if q.strip():
        like = f"%{q.strip()}%"
        sql = ["""SELECT a.*, p.title, p.grandparent_title, p.summary, p.teams, p.section
                  FROM airings a JOIN programs p ON p.guid = a.program_guid
                  WHERE (p.title LIKE ? OR p.grandparent_title LIKE ? OR p.summary LIKE ?)
                    AND a.ends_at > ?"""]
        args = [like, like, like, now]
        if channel:
            sql.append("AND a.channel_vcn = ?")
            args.append(channel)
        if sports:
            sql.append("AND p.section = 'sports'")
        sql.append("ORDER BY a.begins_at LIMIT 400")
        rows = db.query(" ".join(sql), tuple(args))
        channels = db.query(
            "SELECT DISTINCT channel_vcn AS vcn, channel_call_sign AS cs FROM airings "
            "WHERE channel_vcn IS NOT NULL ORDER BY CAST(channel_vcn AS REAL)")
        return page(request, "guide.html", rows=rows, days=[], day="",
                    channels=channels, channel=channel, sports=sports, q=q)

    start = now
    if day:
        try:
            d = datetime.datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=tz())
            start = int(d.timestamp())
        except ValueError:
            pass
    end = start + 86400

    sql = ["""SELECT a.*, p.title, p.grandparent_title, p.summary, p.teams, p.section
              FROM airings a JOIN programs p ON p.guid = a.program_guid
              WHERE a.ends_at > ? AND a.begins_at < ?"""]
    args = [start, end]
    if channel:
        sql.append("AND a.channel_vcn = ?")
        args.append(channel)
    if sports:
        sql.append("AND p.section = 'sports'")
    sql.append("ORDER BY a.begins_at, CAST(a.channel_vcn AS REAL) LIMIT 800")
    rows = db.query(" ".join(sql), tuple(args))

    days = []
    base = datetime.datetime.fromtimestamp(now, tz()).replace(hour=0, minute=0, second=0, microsecond=0)
    for i in range(12):
        d = base + datetime.timedelta(days=i)
        days.append({"key": d.strftime("%Y-%m-%d"),
                     "label": "Today" if i == 0 else d.strftime("%a %-d")})

    channels = db.query(
        "SELECT DISTINCT channel_vcn AS vcn, channel_call_sign AS cs FROM airings "
        "WHERE channel_vcn IS NOT NULL ORDER BY CAST(channel_vcn AS REAL)")
    return page(request, "guide.html", rows=rows, days=days, day=day or days[0]["key"],
                channels=channels, channel=channel, sports=sports, q="")


@app.get("/search")
def search_redirect(q: str = ""):
    """Search used to be its own tab. Keep the URL working."""
    return RedirectResponse(f"/?q={q}" if q else "/", status_code=307)


# ---------- recordings ----------

@app.get("/recordings", response_class=HTMLResponse)
def recordings(request: Request):
    subs = db.query("SELECT * FROM plex_subscriptions ORDER BY title")
    grabs = db.query("SELECT * FROM plex_grabs ORDER BY COALESCE(begins_at, 0)")
    return page(request, "recordings.html", subs=subs, grabs=grabs)


# ---------- passes ----------

@app.get("/passes", response_class=HTMLResponse)
def passes_page(request: Request):
    rows = db.query("SELECT * FROM passes ORDER BY team_name")
    teams = db.query("SELECT * FROM teams ORDER BY name")
    actions = db.query(
        "SELECT a.*, p.team_name FROM pass_actions a LEFT JOIN passes p ON p.id = a.pass_id "
        "ORDER BY a.id DESC LIMIT 100")
    upcoming = []
    for p in rows:
        if not p["enabled"]:
            continue
        for guid, airings in passes.group_by_game(passes.candidate_airings(p["team_id"])).items():
            pick, reason = passes.choose_airing(airings)
            if not pick:
                continue
            upcoming.append({
                "team": p["team_name"], "title": pick["title"],
                "grandparent": pick["grandparent_title"],
                "channel": f'{pick["channel_vcn"]} {pick["channel_call_sign"] or ""}'.strip(),
                "begins_at": pick["begins_at"], "reason": reason,
                "alternatives": len(airings) - 1,
                "blocked": passes.already_handled(guid, pick["id"]),
            })
    upcoming.sort(key=lambda x: x["begins_at"])
    return page(request, "passes.html", passes=rows, teams=teams, actions=actions,
                upcoming=upcoming)


@app.post("/passes/add")
def pass_add(team_id: int = Form(...)):
    t = db.one("SELECT * FROM teams WHERE id = ?", (team_id,))
    if t:
        with db.tx() as c:
            c.execute("INSERT INTO passes (kind, team_id, team_name, enabled, created_at) "
                      "VALUES ('team', ?, ?, 1, ?)", (t["id"], t["name"], int(time.time())))
    return RedirectResponse("/passes", status_code=303)


@app.post("/passes/{pass_id}/delete")
def pass_delete(pass_id: int):
    with db.tx() as c:
        c.execute("DELETE FROM passes WHERE id = ?", (pass_id,))
    return RedirectResponse("/passes", status_code=303)


@app.post("/passes/{pass_id}/toggle")
def pass_toggle(pass_id: int):
    with db.tx() as c:
        c.execute("UPDATE passes SET enabled = 1 - enabled WHERE id = ?", (pass_id,))
    return RedirectResponse("/passes", status_code=303)


@app.post("/passes/run")
async def passes_run():
    await asyncio.to_thread(passes.run_passes)
    return RedirectResponse("/passes", status_code=303)


# ---------- settings ----------

@app.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, tested: str = ""):
    zones = sorted(z for z in zoneinfo.available_timezones() if "/" in z)
    return page(request, "settings.html", zones=zones, tested=tested)


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


@app.post("/sync")
async def sync_now():
    await asyncio.to_thread(sync.full_sync)
    await asyncio.to_thread(passes.run_passes)
    return RedirectResponse("/", status_code=303)


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
    })
