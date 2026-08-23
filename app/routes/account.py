"""First run, sign in, sign out, theme."""
import asyncio
import urllib.parse

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import auth, db, sync
from ._shared import ZONES, current_user, page
from .settings import _test_plex

router = APIRouter()

# ---------- first run ----------

def _configured():
    return bool(db.get_setting("plex_url") and db.get_setting("plex_token"))


@router.get("/welcome", response_class=HTMLResponse)
def welcome(request: Request):
    if _configured():
        return RedirectResponse("/", status_code=303)
    return page(request, "welcome.html", zones=ZONES, nav="")


@router.post("/welcome")
async def welcome_save(plex_url: str = Form(""), plex_token: str = Form(""),
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
    # Pull the guide now rather than waiting for the loop's next turn. Without
    # this a new install lands on an empty grid and has no idea whether it is
    # broken or just early.
    asyncio.create_task(asyncio.to_thread(sync.full_sync))
    return JSONResponse({"ok": True, "detail": detail})


# ---------- sign in ----------

def _set_session(resp, token):
    resp.set_cookie(auth.SESSION_COOKIE, token, httponly=True, samesite="lax",
                    max_age=auth.SESSION_TTL, path="/")
    return resp


@router.get("/setup", response_class=HTMLResponse)
def setup_form(request: Request, error: str = ""):
    if auth.mode() == "none" or not auth.needs_setup():
        return RedirectResponse("/", status_code=303)
    return page(request, "signin.html", setup=True, error=error, nav="")


@router.post("/setup")
def setup_save(username: str = Form(""), password: str = Form("")):
    if auth.mode() == "none" or not auth.needs_setup():
        return RedirectResponse("/", status_code=303)
    try:
        uid = auth.create_user(username, password, role="admin")
    except ValueError as e:
        return RedirectResponse(f"/setup?error={urllib.parse.quote(str(e))}",
                                status_code=303)
    return _set_session(RedirectResponse("/", status_code=303), auth.create_session(uid))


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, error: str = ""):
    if auth.mode() == "none":
        return RedirectResponse("/", status_code=303)
    if auth.needs_setup():
        return RedirectResponse("/setup", status_code=303)
    if current_user(request):
        return RedirectResponse("/", status_code=303)
    return page(request, "signin.html", setup=False, error=error, nav="")


@router.post("/login")
def login_save(username: str = Form(""), password: str = Form("")):
    user = auth.verify(username, password)
    if not user:
        return RedirectResponse(
            "/login?error=" + urllib.parse.quote("that username and password do not match"),
            status_code=303)
    return _set_session(RedirectResponse("/", status_code=303),
                        auth.create_session(user["id"]))


@router.post("/logout")
def logout(request: Request):
    auth.delete_session(request.cookies.get(auth.SESSION_COOKIE))
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.SESSION_COOKIE, path="/")
    return resp


@router.post("/api/theme")
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


