"""Filter tokens shared by the guide grid and the facet panel.

Tokens follow the Ludodex shape: a bare word for a flag, `kind:value` for
anything drawn from the data. They arrive as two comma-separated lists,
include and exclude, and exclude always wins.
"""
from . import db

FLAGS = [
    ("live", "Live broadcast", "a.premiere = 1"),
    ("drm", "DRM encrypted", "a.drm = 1"),
    ("hd", "HD", "a.resolution >= '720'"),
    ("recording", "Being recorded", "a.id IN (SELECT airing_id FROM our_grabs)"),
    ("movie", "Film", "p.section = 'movies'"),
    ("sport", "Sport", "p.section = 'sports'"),
    ("show", "Series", "p.section = 'shows'"),
]
FLAG_SQL = {k: sql for k, _, sql in FLAGS}


def _clause(token):
    """SQL for one token, or None if it means nothing."""
    if token in FLAG_SQL:
        return FLAG_SQL[token], ()
    kind, _, value = token.partition(":")
    if not value:
        return None
    if kind == "channel":
        return "a.channel_vcn = ?", (value,)
    if kind == "genre":
        # genres are stored as a JSON array of strings
        return "p.genres LIKE ?", (f'%"{value}"%',)
    if kind == "team":
        return "p.teams LIKE ?", (f'%"id":{value},%',)
    return None


def build(include, exclude):
    """Return (sql_fragments, args) to AND into a guide query."""
    frags, args = [], []
    for t in include or []:
        c = _clause(t.strip())
        if not c:
            continue
        frags.append("AND (" + c[0] + ")")
        args += list(c[1])
    for t in exclude or []:
        c = _clause(t.strip())
        if not c:
            continue
        # NOT (...) alone would drop rows where the column is NULL, so guard it.
        frags.append("AND NOT COALESCE((" + c[0] + "), 0)")
        args += list(c[1])
    return frags, args


def parse(value):
    return [t for t in (value or "").split(",") if t.strip()]


def facets():
    """Everything the filter panel offers, with counts, grouped into sections."""
    def rows(sql, prefix=None):
        out = []
        for r in db.query(sql):
            ident = r["id"]
            out.append({"id": f"{prefix}:{ident}" if prefix else ident,
                        "name": r["name"], "count": r["n"]})
        return out

    channels = rows(
        """SELECT c.vcn AS id, COALESCE(NULLIF(c.call_sign,''), c.vcn) AS name,
                  COUNT(a.id) AS n
           FROM channels c LEFT JOIN airings a ON a.channel_vcn = c.vcn
           GROUP BY c.vcn HAVING n > 0 ORDER BY CAST(c.vcn AS REAL)""", "channel")
    logos = {r["vcn"]: bool(r["logo_path"]) for r in db.query(
        "SELECT vcn, logo_path FROM channels")}
    for c in channels:
        vcn = c["id"].split(":", 1)[1]
        c["logo"] = logos.get(vcn, False)
        c["name"] = f"{vcn} {c['name']}".strip()

    # Genres and teams live in JSON columns, so they are counted in Python.
    from collections import Counter
    gc, tc = Counter(), Counter()
    tnames = {}
    for r in db.query("SELECT genres, teams FROM programs"):
        for g in db.unjs(r["genres"]):
            if g:
                gc[g] += 1
        for t in db.unjs(r["teams"]):
            if t.get("id") is not None:
                tc[t["id"]] += 1
                tnames[t["id"]] = t.get("name") or str(t["id"])

    flags = []
    for key, label, sql in FLAGS:
        n = db.one(f"SELECT COUNT(*) n FROM airings a "
                   f"JOIN programs p ON p.guid = a.program_guid WHERE {sql}")["n"]
        if n:
            flags.append({"id": key, "name": label, "count": n})

    return [
        {"title": "Kind", "rows": flags},
        {"title": "Channels", "rows": channels},
        {"title": "Genres", "rows": sorted(
            [{"id": f"genre:{g}", "name": g, "count": n} for g, n in gc.items()],
            key=lambda r: r["name"].lower())},
        {"title": "Teams", "rows": sorted(
            [{"id": f"team:{i}", "name": tnames[i], "count": n} for i, n in tc.items()],
            key=lambda r: r["name"].lower())},
    ]
