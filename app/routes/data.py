"""Export, import, scheduled backups, the backing store."""
import asyncio
import datetime

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from .. import backingstore, backups, db, dbstore, portable
from ._shared import VERSION

router = APIRouter()

# ---------- database: export, import, snapshots, backing store ----------

@router.get("/api/export")
def api_export(secrets: int = 0):
    """Download everything you decided, in one file."""
    blob = portable.export_bytes(include_secrets=bool(secrets), version=VERSION)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return Response(
        blob, media_type="application/zip",
        headers={"Content-Disposition":
                 f'attachment; filename="couchelephant-{stamp}.zip"'})


@router.post("/api/import/inspect")
async def api_import_inspect(file: UploadFile = File(...)):
    """What is in this file, before anything is written."""
    try:
        return JSONResponse({"ok": True, **portable.describe(await file.read())})
    except portable.ImportError_ as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@router.post("/api/import")
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


@router.get("/api/backups/jobs")
def api_backup_jobs():
    return JSONResponse({"ok": True, "jobs": backups.jobs()})


@router.post("/api/backups/jobs")
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


@router.post("/api/backups/jobs/{job_id}/delete")
def api_backup_job_delete(job_id: int):
    backups.delete_job(job_id)
    return JSONResponse({"ok": True, "jobs": backups.jobs()})


@router.post("/api/backups/jobs/{job_id}/run")
async def api_backup_job_run(job_id: int):
    try:
        out = await asyncio.to_thread(backups.run_job, job_id, VERSION)
    except FileNotFoundError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=404)
    out["jobs"] = backups.jobs()
    return JSONResponse(out, status_code=200 if out.get("ok") else 400)


@router.get("/api/backups/archives")
def api_backup_archives(dest: str = ""):
    return JSONResponse({"ok": True, "archives": backups.archives(dest)})


@router.post("/api/backups/restore")
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


@router.get("/api/backingstore/config")
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


@router.post("/api/backingstore/config")
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


@router.post("/api/backingstore/test")
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


@router.post("/api/backingstore/run")
async def api_backingstore_run(dry_run: str = Form("")):
    try:
        out = await asyncio.to_thread(
            backingstore.sync_all, str(dry_run).lower() in ("1", "true", "yes"))
    except backingstore.BackendError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return JSONResponse(out)


@router.post("/api/backingstore/restore")
async def api_backingstore_restore(dry_run: str = Form("")):
    try:
        out = await asyncio.to_thread(
            backingstore.restore_from_remote,
            str(dry_run).lower() in ("1", "true", "yes"))
    except backingstore.BackendError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    return JSONResponse(out)


@router.get("/api/backingstore/status")
def api_backingstore_status():
    return JSONResponse({"ok": True, "status": backingstore.status(),
                         "configured": backingstore.chosen() is not None})


