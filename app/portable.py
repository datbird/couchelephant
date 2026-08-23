"""Export and import: everything you decided, in one file you can move.

The file is an ordinary zip. Open it and the contents are readable JSON, on
purpose: a backup only its own program can read is not much of a backup, and
being able to look at what you are about to restore is worth more than a
clever format.

    couchelephant.json      the durable stores, plus what made the file
    logos/<vcn>.png         the channel artwork you supplied

The guide is not in it. Programmes, airings, channels and Plex's own schedule
are a cache that rebuilds from your server in seconds, so carrying a stale copy
would be larger, slower and wrong.

The Plex token is not in it either, unless you ask. It is a credential, and an
export is a file that ends up in an email.
"""
import io
import json
import os
import time
import zipfile

from . import db, dbstore

FORMAT = 1
MANIFEST = "couchelephant.json"
MAX_IMPORT = 64 * 1024 * 1024      # a durable export is kilobytes, not megabytes


class ImportError_(ValueError):
    """A file that cannot be imported. The message is shown to the user."""


def _logo_dir():
    return os.environ.get("COUCHELEPHANT_LOGOS", "/data/logos")


def export_bytes(include_secrets: bool = False, version: str = "", note: str = "") -> bytes:
    """The whole export, as bytes ready to send."""
    stores = dbstore.snapshot(include_secrets=include_secrets)
    manifest = {
        "format": FORMAT,
        "app": "couchelephant",
        "version": version,
        "created_at": int(time.time()),
        "note": note,
        "includes_secrets": bool(include_secrets),
        "counts": {name: len(rows) for name, rows in stores.items()},
        "stores": {name: list(rows.values()) for name, rows in stores.items()},
    }

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(MANIFEST, json.dumps(manifest, indent=1, sort_keys=True))
        # The artwork itself, not just the row that names it. A restore with
        # no image files is a list of broken channel logos.
        for rec in stores.get("channel_art", {}).values():
            name = os.path.basename(rec.get("custom_logo") or "")
            if not name:
                continue
            path = os.path.join(_logo_dir(), name)
            if os.path.isfile(path):
                z.write(path, f"logos/{name}")
    return buf.getvalue()


def describe(blob: bytes) -> dict:
    """What is in this file, without changing anything. For the confirm step."""
    manifest = _manifest(blob)
    return {
        "format": manifest.get("format"),
        "created_at": manifest.get("created_at"),
        "version": manifest.get("version", ""),
        "note": manifest.get("note", ""),
        "includes_secrets": bool(manifest.get("includes_secrets")),
        "counts": {k: len(v) for k, v in (manifest.get("stores") or {}).items()},
    }


def _manifest(blob):
    if len(blob) > MAX_IMPORT:
        raise ImportError_(f"that file is larger than the {MAX_IMPORT // 1048576}MB "
                           "limit. An export is normally a few kilobytes.")
    try:
        z = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile:
        raise ImportError_("that is not a CouchElephant export. It is not a zip file.") from None
    with z:
        if MANIFEST not in z.namelist():
            raise ImportError_("that zip has no CouchElephant export in it.")
        try:
            manifest = json.loads(z.read(MANIFEST))
        except ValueError:
            raise ImportError_("the export inside that file is damaged.") from None
    if manifest.get("app") != "couchelephant":
        raise ImportError_("that export was made by a different program.")
    if int(manifest.get("format") or 0) > FORMAT:
        raise ImportError_("that export came from a newer CouchElephant. "
                           "Upgrade this one first.")
    if not isinstance(manifest.get("stores"), dict):
        raise ImportError_("the export inside that file has no data in it.")
    return manifest


def import_bytes(blob: bytes, replace: bool = False, include_secrets: bool = True) -> dict:
    """Read an export back in.

    `replace` deletes what the file does not carry, so the result is exactly
    what was exported. Off, it merges: everything in the file is written and
    anything else here is left alone.

    Returns a per-store report.
    """
    manifest = _manifest(blob)
    stores = manifest["stores"]

    report, unknown = {}, []
    for name, rows in stores.items():
        if name not in dbstore.STORES:
            # A newer export with a store this version has never heard of.
            # Named rather than dropped in silence.
            unknown.append(name)
            continue
        if not isinstance(rows, list):
            raise ImportError_(f"the {name} section of that export is damaged.")
        if name == "settings" and not include_secrets:
            rows = [r for r in rows if r.get("key") not in dbstore.SECRET_SETTINGS]
        written, deleted = dbstore.apply(name, rows, delete_missing=replace)
        report[name] = {"written": written, "deleted": deleted}

    # The artwork files, then the rows that point at them.
    written_logos = _restore_logos(blob)
    # A grab names its pass by uid, which says nothing about this machine's
    # own ids until they are linked up.
    dbstore.relink_passes()
    db.init()          # any column the incoming rows did not carry

    return {"ok": True, "stores": report, "logos": written_logos,
            "unknown_stores": unknown,
            "secrets": bool(manifest.get("includes_secrets")) and include_secrets}


def _restore_logos(blob):
    out = _logo_dir()
    os.makedirs(out, exist_ok=True)
    n = 0
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        for entry in z.namelist():
            if not entry.startswith("logos/") or entry.endswith("/"):
                continue
            name = os.path.basename(entry)
            # A zip can name a path that walks out of where you unpack it.
            if not name or name in (".", "..") or "/" in name or "\\" in name:
                continue
            data = z.read(entry)
            if len(data) > 4 * 1024 * 1024:
                continue
            with open(os.path.join(out, name), "wb") as f:
                f.write(data)
            n += 1
    return n
