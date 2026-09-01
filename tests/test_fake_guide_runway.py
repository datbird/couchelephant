"""The fake guide has to stay ahead of the suite that reads it.

This is a guard on the test harness itself, which is the one thing no other
test can catch.

`fake_plex.LIVE_AT` used to round up to the next 1800-second boundary. That
gives a run anywhere between thirty minutes of runway and none at all: start at
:29:50 and the live game has kicked off before the tests reach it. Everything
that reads a FUTURE booking then finds no rows and reports zero of everything,
which reads exactly like the feature being broken.

It cost a release. On 2026-09-01 the publish job for v1.0.5 failed on
`test_a_full_sync_runs_the_check` at 15:30:07 against an anchor of 15:30:00,
while CI on the identical commit had passed minutes before. The code was fine
both times.

So the runway is asserted rather than assumed. If someone trims the anchor back,
this fails immediately and by name, instead of a random unrelated test failing
on one run in ten.
"""
import time

from app import verify
from tests import fake_plex

# The whole suite runs in about three minutes. Ten is the margin that makes a
# slow runner a non-event; below it, the harness is gambling.
MINIMUM_RUNWAY = 10 * 60


def test_the_live_game_has_not_started_yet():
    assert fake_plex.LIVE_AT > time.time(), (
        "the fake guide's live game is already in the past at import; every "
        "check that reads a future booking will silently find nothing")


def test_there_is_enough_runway_for_a_slow_run():
    runway = fake_plex.LIVE_AT - time.time()
    assert runway >= MINIMUM_RUNWAY, (
        f"only {int(runway)}s of runway; a slow runner will cross the kickoff "
        f"mid-suite and tests will fail for the clock, not the code")


def test_every_derived_time_is_after_the_anchor():
    """They are offsets from LIVE_AT, so a sign slip would put one behind it."""
    for name in ("REPEAT_AT", "EPISODE_AT", "DRM_AT", "NO_TEAM_AT"):
        assert getattr(fake_plex, name) > fake_plex.LIVE_AT, name


def test_the_live_game_is_still_too_close_to_repair():
    """The anchor must not drift so far ahead that it lands OUTSIDE the repair
    lead. Tests that want the repair path move the game themselves, and they
    prove something only while the default sits inside the guard."""
    assert not verify.can_repair(begins_at=fake_plex.LIVE_AT,
                                 now=int(time.time()))
