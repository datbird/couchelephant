"""Waving off a suggestion.

Its own module because it is one route with one rule, and that rule is worth
being able to find: only a tip can be dismissed. A health problem is answered
by fixing it, not by clicking it away.
"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from .. import health

router = APIRouter()


@router.post("/api/notices/{code}/dismiss")
def api_dismiss(code: str):
    """Refuse anything that is not a tip.

    The guard lives in `health.dismiss` as well. Two layers on purpose: a UI
    that grew a dismiss button on the wrong kind of notice would still be
    refused here.
    """
    if health.dismiss(code):
        return JSONResponse({"ok": True})
    return JSONResponse(
        {"ok": False, "error": "That notice cannot be dismissed."},
        status_code=400)
