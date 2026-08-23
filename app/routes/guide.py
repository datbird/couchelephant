"""The guide: the grid, a programme, search, channel logos."""
import datetime
import os
import time
import urllib.parse

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

from .. import db, filters, smartfilter
from ._shared import _logo_map, page, templates, tz

router = APIRouter()

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
        like = f"%{smartfilter.like(q.strip())}%"
        sql.append("""WHERE (p.title LIKE ? ESCAPE '\\' OR p.grandparent_title LIKE ? ESCAPE '\\'
                            OR p.summary LIKE ? ESCAPE '\\')
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


@router.get("/api/grid")
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


@router.get("/api/facets")
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


@router.get("/api/program")
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


@router.get("/partial/airings", response_class=HTMLResponse)
def airings_partial(request: Request, day: str = "", channel: str = "", sports: int = 0,
                    q: str = "", offset: int = 0, f: str = "", x: str = ""):
    """Just the rows, for infinite scroll. Empty response means the end."""
    rows = _airings_query(day, channel, sports, q, offset, PAGE_SIZE, f, x)
    if not rows:
        return HTMLResponse("")
    return templates.TemplateResponse(
        "_rows.html", {"request": request, "rows": rows, "q": q, "logos": _logo_map()})


@router.get("/", response_class=HTMLResponse)
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


@router.get("/search")
def search_redirect(q: str = ""):
    """Search used to be its own tab. Keep the URL working."""
    return RedirectResponse(f"/?q={urllib.parse.quote(q)}" if q else "/", status_code=307)



# A 1x1 transparent PNG, so a missing logo renders as empty space rather than
# a broken-image glyph.
_BLANK = bytes.fromhex(
    "89504e470d0a1a0a0000000d494844520000000100000001080600000"
    "01f15c4890000000a49444154789c6360000002000100ffff03000006000557bfabd4"
    "0000000049454e44ae426082")


_LOGO_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
               ".gif": "image/gif", ".webp": "image/webp"}


@router.get("/logo/{vcn}")
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


