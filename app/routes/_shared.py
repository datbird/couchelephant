"""What every route module needs: the template engine, the page helper,
who is asking, and the configured Plex client."""
import datetime
import os
import time
import zoneinfo

from fastapi.templating import Jinja2Templates

from .. import auth, cf_access, db
from ..plex import Plex

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
templates = Jinja2Templates(directory=os.path.join(BASE, "templates"))
VERSION = "1.0.0"

# What the browser should call this build of the scripts and the stylesheet.
# The version alone is not enough: it does not change between deploys, so a
# fix would sit on the server while the browser served yesterday's copy from
# cache. That happened, and the fix looked like it had not been made.
def _asset_version():
    here = os.path.dirname(os.path.abspath(__file__))
    newest = 0
    for root, _dirs, files in os.walk(os.path.join(here, "static")):
        for f in files:
            try:
                newest = max(newest, int(os.path.getmtime(os.path.join(root, f))))
            except OSError:
                pass
    # Some pages still carry their own script and style, so they count too.
    for f in ("templates/base.html", "templates/recordings.html",
              "templates/guide.html", "templates/_settings.html"):
        try:
            newest = max(newest, int(os.path.getmtime(os.path.join(here, f))))
        except OSError:
            pass
    return f"{VERSION}-{newest}"


ASSET_V = _asset_version()

# Walking the tzdata directory on every settings render costs more than the
# list is worth. It cannot change while the process runs.
# UTC first: it is the default, and a select whose default is not in its list
# quietly shows the first entry instead (Africa/Abidjan, once, in a screenshot).
ZONES = ["UTC"] + sorted(z for z in zoneinfo.available_timezones() if "/" in z)



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


def _int(v, default=0):
    """A form field as an int, or the default. Never a 500 for a bad query."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _plex():
    """The configured server. Use it as a context manager, so the connection
    pool is closed when the request is done rather than when the garbage
    collector gets round to it."""
    return Plex(db.get_setting("plex_url"), db.get_setting("plex_token"))


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
    ctx.setdefault("asset_v", ASSET_V)
    ctx["request"] = request
    return templates.TemplateResponse(name, ctx)


# Reachable without signing in: the sign-in screens themselves, the health
# check, and the static files that render them.
