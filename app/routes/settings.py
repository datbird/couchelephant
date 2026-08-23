"""Settings: the Plex connection, accounts, channel artwork, sync."""
import asyncio
import os
import time
import urllib.parse

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from .. import auth, cf_access, db, passes, sync
from ..plex import Plex, PlexError
from ._shared import ZONES, current_user, page

router = APIRouter()

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


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, tested: str = "", autherror: str = ""):
    return page(request, "settings.html", zones=ZONES, tested=tested,
                autherror=autherror, logos=sync.logo_coverage(),
                channels=_channel_rows(), users=auth.list_users(),
                cf_ready=cf_access.available())


@router.get("/partial/settings", response_class=HTMLResponse)
def settings_partial(request: Request, tested: str = "", autherror: str = ""):
    """The settings window on its own, for the overlay the gear opens."""
    return page(request, "_settings.html", zones=ZONES, tested=tested,
                autherror=autherror, logos=sync.logo_coverage(),
                channels=_channel_rows(), users=auth.list_users(),
                cf_ready=cf_access.available())


@router.post("/settings")
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


@router.post("/settings/auth")
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


@router.post("/settings/users/{uid}/delete")
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

    with Plex(url, token) as plex:
        return _probe(plex, url)


def _probe(plex, url):
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


@router.post("/settings/test")
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


@router.post("/settings/channels/{vcn}/logo")
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
    safe = "".join(c if (c.isalnum() and c.isascii()) or c in "._-" else "_" for c in vcn)
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


@router.post("/settings/channels/{vcn}/logo/reset")
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


@router.post("/settings/logos")
async def refetch_logos():
    """Force every channel logo to be downloaded again."""
    await asyncio.to_thread(sync.cache_logos, True)
    return RedirectResponse("/settings", status_code=303)


@router.post("/sync")
async def sync_now():
    await asyncio.to_thread(sync.full_sync)
    await asyncio.to_thread(passes.run_passes)
    return RedirectResponse("/", status_code=303)


