"""Does the recording Plex is holding still match the pass that booked it?

A pass books a game once and then stops looking at it. Change the pass
afterwards, and the booking keeps the settings it was made with for ever.
Nothing errors: the pass ran, the recording exists, and the game is cut off at
the end. That happened here, and the whole of `passes.already_handled` is why.
It asks "did we book this game" and never "is that booking still right".

So this compares, every sync, what Plex is holding against what the pass says
now. Four things have to be true, and the last two are the ones people forget:

  1. Plex still holds the subscription at all.
  2. Plex has actually scheduled a recording for it.
  3. Every setting the pass carries matches Plex's copy.
  4. The pin still names the airing the pass chose.

The difficulty is not spotting a difference. It is refusing to invent one.
Plex answers `oneShot` as the string 'true' to a '1' we sent, returns numbers
as ints on one payload and strings on another, and omits a setting rather than
reporting it empty. A comparison that counted any of those as a difference
would cancel and re-book the same recording on every sync, for ever, against a
live DVR. `same` and the `unchecked` verdict exist for exactly that.
"""

# What a pinned one-shot must always say, whatever the pass stores. These are
# the mechanism the app exists for, so a pass may not assert its own values:
# `wanted` overwrites them from the airing that was actually chosen.
PINNED = ("oneShot", "lineupChannel", "startTimeslot")

# Repair cancels the recording and books it again. There is a moment in the
# middle with nothing scheduled, so it is only ever done with room to spare:
# far enough out that a failed re-book is caught by a later sync rather than
# discovered at kickoff. Never less than half an hour whatever the interval.
MINIMUM_LEAD = 1800
SYNCS_OF_SLACK = 2

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _as_int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _as_bool(v):
    s = str(v).strip().lower()
    if s in _TRUE:
        return True
    if s in _FALSE:
        return False
    return None


def same(want, have) -> bool:
    """Whether Plex's value means what ours means.

    Numbers first, then booleans, then the plain text. Numbers first matters:
    '30' and 30 are the same padding, and settling that as text would report
    drift on every server that answers with ints.

    Booleans second is what absorbs Plex's own quirk. `oneShot` goes out as
    '1' and comes back as 'true', which is not a number on both sides, so it
    falls through to the boolean reading and agrees. Nothing else does: '30'
    against '0' is decided as numbers and rightly differs.
    """
    if want is None or have is None:
        return want is None and have is None
    wi, hi = _as_int(want), _as_int(have)
    if wi is not None and hi is not None:
        return wi == hi
    wb, hb = _as_bool(want), _as_bool(have)
    if wb is not None and hb is not None:
        return wb == hb
    return str(want).strip() == str(have).strip()


def wanted(prefs: dict | None, airing) -> dict:
    """What this booking should look like, given the pass and the airing.

    The pin comes from the airing, never from the pass. A pass that somehow
    carries a stale channel must not be able to assert it over the broadcast
    it actually chose, and the guide can move a game after it was booked.
    """
    want = {k: v for k, v in (prefs or {}).items() if k not in PINNED}
    want["oneShot"] = "1"
    want["lineupChannel"] = airing["channel_identifier"] or ""
    want["startTimeslot"] = str(airing["begins_at"])
    return want


def compare(*, want: dict, have: dict | None) -> list[dict]:
    """Every way Plex's copy differs from what the pass says. Empty is agreement.

    Each entry carries a `kind`:

      `missing`    Plex is not holding this recording at all.
      `differs`    Plex holds a different value, and we can prove it.
      `unchecked`  Plex did not report the setting, so it cannot be compared.

    `unchecked` is a verdict rather than a silence, and it deliberately does
    not ask for a repair. A field Plex never reports would be repaired, still
    not reported, and repaired again on every sync until someone noticed. Not
    knowing is not the same as disagreeing, and only the second is grounds for
    cancelling a recording.
    """
    if have is None:
        return [{"field": "subscription", "kind": "missing",
                 "want": "a scheduled recording", "have": "nothing"}]
    out = []
    for key in sorted(want):
        if key not in have:
            out.append({"field": key, "kind": "unchecked",
                        "want": str(want[key]), "have": "not reported"})
        elif not same(want[key], have[key]):
            out.append({"field": key, "kind": "differs",
                        "want": str(want[key]), "have": str(have[key])})
    return out


def no_recording(field: str = "recording") -> list[dict]:
    """Plex holds the rule but has scheduled nothing against it.

    Its own kind of wrong. The subscription reads fine and every setting
    agrees, so a check that stopped at the settings would call this healthy.
    """
    return [{"field": field, "kind": "missing",
             "want": "a scheduled recording", "have": "nothing scheduled"}]


def needs_repair(diffs: list[dict]) -> bool:
    """Whether these differences are worth cancelling a recording over."""
    return any(d["kind"] in ("missing", "differs") for d in diffs)


def describe(diffs: list[dict]) -> str:
    """The differences in the words a person would use."""
    bits = []
    for d in diffs:
        if d["kind"] == "missing":
            bits.append(f"{d['field']}: {d['have']}")
        elif d["kind"] == "differs":
            bits.append(f"{d['field']}: Plex has {d['have']}, the pass says {d['want']}")
    return "; ".join(bits)


def repair_lead(sync_minutes: int = 60) -> int:
    """How much room a repair needs before the broadcast starts.

    Two sync intervals, so a re-book that fails still has a later sync to put
    it right, and never under half an hour however often the app syncs.
    """
    return max(int(sync_minutes) * 60 * SYNCS_OF_SLACK, MINIMUM_LEAD)


def can_repair(*, begins_at: int, now: int, lead: int | None = None) -> bool:
    """Whether there is room to cancel and re-book this safely.

    A recording that has started, or is about to, is left exactly as it is.
    Wrong padding on a game you are recording beats no recording of it.
    """
    if not begins_at:
        return False
    return (begins_at - now) > (repair_lead() if lead is None else lead)
