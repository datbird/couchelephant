"""The smart filter: what it compiles to, and what it refuses."""
import pytest

from app import db, passes, smartfilter
from tests import fake_plex


def _ids(tree):
    return sorted(r["program_guid"] for r in passes.smart_airings(tree))


def _titles(tree):
    return sorted({r["title"] for r in passes.smart_airings(tree)})


# ---- compiling ----

def test_a_condition_becomes_parameterised_sql():
    sql, args = smartfilter.build({"field": "title", "cmp": "is", "value": "x"})
    assert "?" in sql
    assert args == ["x"]
    assert "x" not in sql, "a value must never reach the query text"


def test_a_field_name_is_looked_up_not_interpolated():
    with pytest.raises(smartfilter.FilterError):
        smartfilter.build({"field": "title) OR 1=1 --", "cmp": "is", "value": "x"})


def test_a_field_cannot_be_asked_something_it_has_no_answer_to():
    with pytest.raises(smartfilter.FilterError):
        smartfilter.build({"field": "year", "cmp": "contains", "value": "2"})


def test_a_number_field_refuses_words():
    with pytest.raises(smartfilter.FilterError):
        smartfilter.build({"field": "year", "cmp": "gt", "value": "soon"})


def test_a_date_field_wants_a_date():
    with pytest.raises(smartfilter.FilterError):
        smartfilter.build({"field": "aired", "cmp": "after", "value": "last tuesday"})
    smartfilter.build({"field": "aired", "cmp": "after", "value": "2020-01-01"})


def test_an_empty_group_is_refused_rather_than_matching_everything():
    with pytest.raises(smartfilter.FilterError):
        smartfilter.build({"op": "all", "nodes": []})


def test_a_tree_cannot_grow_without_limit():
    deep = {"field": "hd", "cmp": "yes"}
    for _ in range(smartfilter.MAX_DEPTH + 2):
        deep = {"op": "all", "nodes": [deep]}
    with pytest.raises(smartfilter.FilterError):
        smartfilter.build(deep)

    wide = {"op": "any", "nodes": [{"field": "hd", "cmp": "yes"}] * (smartfilter.MAX_NODES + 2)}
    with pytest.raises(smartfilter.FilterError):
        smartfilter.build(wide)


def test_a_genre_match_does_not_match_a_longer_genre():
    """The quotes are part of the pattern, or "Drama" also finds "Docudrama"."""
    sql, args = smartfilter.build({"field": "genre", "cmp": "is", "value": "Drama"})
    assert args == ['%"Drama"%']


# ---- running against the guide ----

def test_one_condition_selects_by_genre(synced):
    assert _titles({"field": "genre", "cmp": "is", "value": "Football"}) == \
        ["Chiefs at Buccaneers"]


def test_all_means_every_condition(synced):
    tree = {"op": "all", "nodes": [
        {"field": "genre", "cmp": "is", "value": "Comedy"},
        {"field": "rating", "cmp": "is", "value": "TV-PG"}]}
    assert _titles(tree) == ["Quiz Night"]


def test_any_means_one_of_them(synced):
    tree = {"op": "any", "nodes": [
        {"field": "genre", "cmp": "is", "value": "Football"},
        {"field": "genre", "cmp": "is", "value": "Comedy"}]}
    assert _titles(tree) == ["Chiefs at Buccaneers", "Quiz Night"]


def test_none_excludes(synced):
    tree = {"op": "all", "nodes": [
        {"field": "hd", "cmp": "yes"},
        {"op": "none", "nodes": [{"field": "genre", "cmp": "is", "value": "Football"}]}]}
    assert "Chiefs at Buccaneers" not in _titles(tree)


def test_groups_nest_to_any_depth(synced):
    tree = {"op": "all", "nodes": [
        {"op": "any", "nodes": [
            {"op": "all", "nodes": [
                {"field": "genre", "cmp": "is", "value": "Comedy"},
                {"field": "year", "cmp": "gt", "value": "2000"}]},
            {"field": "genre", "cmp": "is", "value": "Football"}]}]}
    assert _titles(tree) == ["Chiefs at Buccaneers", "Quiz Night"]


def test_a_blank_does_not_match_a_positive_condition(synced):
    """The game carries no content rating, so it is not TV-PG."""
    got = _titles({"field": "rating", "cmp": "is", "value": "TV-PG"})
    assert got == ["Quiz Night"]


def test_a_blank_does_not_sneak_through_a_negative_condition(synced):
    """B-shaped: NULL fails every SQL comparison, including a negative one, so
    "is not TV-MA" would otherwise quietly admit everything unrated."""
    got = _titles({"field": "rating", "cmp": "!is", "value": "TV-MA"})
    assert got == ["Quiz Night"]
    assert "Chiefs at Buccaneers" not in got


def test_a_blank_is_included_when_the_user_says_so(synced):
    got = _titles({"field": "rating", "cmp": "!is", "value": "TV-MA", "blank": True})
    assert "Chiefs at Buccaneers" in got
    assert "Locked Broadcast" not in got


def test_blank_also_widens_a_positive_condition(synced):
    got = _titles({"field": "rating", "cmp": "is", "value": "TV-PG", "blank": True})
    assert "Chiefs at Buccaneers" in got


def test_text_search_looks_inside_the_description(synced):
    assert _titles({"field": "summary", "cmp": "contains", "value": "game"}) == \
        ["Chiefs at Buccaneers"]


def test_the_length_condition_reads_minutes_not_milliseconds(synced):
    """Plex sends duration in milliseconds. A person types 90."""
    assert _titles({"field": "duration", "cmp": "gt", "value": "100"}) == \
        ["Chiefs at Buccaneers"]
    assert _titles({"field": "duration", "cmp": "lt", "value": "70"}) == ["Quiz Night"]


def test_hd_compares_as_a_number(synced):
    """B2 again, in a second place. '1080' sorts below '720' as text."""
    got = _titles({"field": "hd", "cmp": "yes"})
    assert "Quiz Night" in got, "1080 is HD"


def test_a_broadcast_condition_selects_by_channel(synced):
    rows = passes.smart_airings({"field": "channel", "cmp": "is", "value": "41.1"})
    assert {r["channel_vcn"] for r in rows} == {"41.1"}


def test_the_live_condition_selects_the_premiere(synced):
    rows = passes.smart_airings({"field": "live", "cmp": "yes"})
    assert all(r["premiere"] for r in rows)
    rows = passes.smart_airings({"field": "live", "cmp": "no"})
    assert not any(r["premiere"] for r in rows)


# ---- describing and judging ----

def test_a_filter_describes_itself_in_words():
    tree = {"op": "any", "nodes": [
        {"field": "genre", "cmp": "is", "value": "Comedy"},
        {"field": "genre", "cmp": "is", "value": "Drama"}]}
    assert smartfilter.describe(tree) == "genre is Comedy or genre is Drama"


def test_a_filter_that_names_nothing_about_the_programme_is_loose():
    assert smartfilter.is_loose({"field": "hd", "cmp": "yes"})
    assert smartfilter.is_loose({"op": "all", "nodes": [
        {"field": "channel", "cmp": "is", "value": "41.1"},
        {"field": "live", "cmp": "yes"}]})


def test_a_filter_that_names_the_programme_is_not_loose():
    assert not smartfilter.is_loose({"field": "genre", "cmp": "is", "value": "Comedy"})
    assert not smartfilter.is_loose({"op": "all", "nodes": [
        {"field": "hd", "cmp": "yes"},
        {"field": "title", "cmp": "contains", "value": "Quiz"}]})


def test_a_percent_sign_in_a_title_is_not_a_wildcard():
    frag, args = smartfilter.build({"field": "title", "cmp": "contains", "value": "100% Hot"})
    assert "ESCAPE" in frag
    assert args == ["%100\\% Hot%"]
