"""Guide filter tokens."""
import json

from app import db, filters


def _seed():
    with db.tx() as c:
        c.execute("INSERT INTO programs (guid, title, section, genres, teams) "
                  "VALUES ('g1','A','shows','[\"Drama\"]','[]')")
        c.execute("INSERT INTO programs (guid, title, section, genres, teams) "
                  "VALUES ('g2','B','sports','[\"Football\"]','[{\"id\":236,\"name\":\"Chiefs\"}]')")
        for aid, guid, vcn, res, prem, drm in [
                ("a1", "g1", "9.1", "1080", 0, 0),
                ("a2", "g1", "5.1", "480", 0, 0),
                ("a3", "g2", "41.1", "720", 1, 0),
                ("a4", "g2", "38.1", "2160", 0, 1)]:
            c.execute("""INSERT INTO airings (id, program_guid, channel_vcn, resolution,
                                              premiere, drm, begins_at, ends_at)
                         VALUES (?,?,?,?,?,?,1000,2000)""",
                      (aid, guid, vcn, res, prem, drm))


def _run(include=(), exclude=()):
    frags, args = filters.build(list(include), list(exclude))
    sql = ("SELECT a.id FROM airings a JOIN programs p ON p.guid = a.program_guid "
           "WHERE 1=1 " + " ".join(frags) + " ORDER BY a.id")
    return [r["id"] for r in db.query(sql, tuple(args))]


def test_hd_includes_1080_and_2160():
    """B2. As strings '1080' sorts below '720', so a string compare silently
    dropped every 1080 channel out of HD."""
    _seed()
    assert _run(include=["hd"]) == ["a1", "a3", "a4"]


def test_live_and_drm_flags():
    _seed()
    assert _run(include=["live"]) == ["a3"]
    assert _run(include=["drm"]) == ["a4"]


def test_section_flags():
    _seed()
    assert _run(include=["sport"]) == ["a3", "a4"]
    assert _run(include=["show"]) == ["a1", "a2"]


def test_exclude_wins_and_keeps_null_rows():
    """NOT (...) alone drops rows where the column is NULL."""
    _seed()
    with db.tx() as c:
        c.execute("""INSERT INTO airings (id, program_guid, channel_vcn, resolution,
                                          begins_at, ends_at)
                     VALUES ('a5','g1','9.1',NULL,1000,2000)""")
    got = _run(exclude=["hd"])
    assert "a5" in got, "a row with no resolution is not HD, and must survive"
    assert "a1" not in got


def test_channel_genre_and_team_tokens():
    _seed()
    assert _run(include=["channel:41.1"]) == ["a3"]
    assert _run(include=["genre:Football"]) == ["a3", "a4"]
    assert _run(include=["team:236"]) == ["a3", "a4"]


def test_a_meaningless_token_is_ignored_not_fatal():
    _seed()
    assert _run(include=["nonsense", "channel:"]) == ["a1", "a2", "a3", "a4"]


def test_facets_group_into_sections():
    _seed()
    titles = [s["title"] for s in filters.facets()]
    assert titles == ["Kind", "Channels", "Genres", "Teams"]
