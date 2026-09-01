"""Where CouchElephant looks for things the Plex guide has not heard of.

No guide data anywhere reaches further than about three weeks, the paid sources
included, because no broadcaster has decided that far ahead. What does exist
months out is two other things, and no single source carries both:

  - announcements: a title, a network, and a date that is often only a month
  - published league schedules: real dates and times, before any broadcaster

These modules fetch those. **They never book anything.** Only a guide airing
carries a channel, so only a guide airing can be recorded.
"""
import calendar
import datetime
from dataclasses import dataclass

TIMEOUT = 15.0

# Loosest to tightest, so a caller comparing two answers can keep the better.
PRECISIONS = ("year", "month", "day", "time")

# Tried in order. The first that parses wins, and names how much to believe.
_FORMATS = (
    ("%Y-%m-%dT%H:%M:%S", "time"),
    ("%Y-%m-%d %H:%M:%S", "time"),
    ("%Y-%m-%d %H:%M", "time"),
    ("%Y-%m-%d", "day"),
    ("%Y-%m", "month"),
    ("%Y", "year"),
)


@dataclass(frozen=True)
class Announcement:
    """One thing a source says is coming, at whatever precision it knows."""

    source: str
    source_id: str
    title: str
    subtitle: str | None = None
    network: str | None = None
    expected_at: int | None = None
    precision: str = "year"


def precision_of(text: str | None) -> tuple[int | None, str]:
    """Turn a source's date string into an epoch and an honest precision.

    A source that said "2027-03" did not say the first of March at midnight.
    Reading it that way invents a day and a time, and a reader takes an
    invented date for a real one. So the epoch is the start of the range and
    the precision says how much of it to believe.

    Anything unparseable is not guessed at. It comes back as no date at all.
    """
    text = (text or "").strip()
    if not text:
        return None, "year"
    # An ISO instant WITH A ZONE, which is what TVmaze publishes per episode
    # (`airstamp`, "2027-03-04T01:00:00+00:00"). None of the formats below
    # match one, so before this it fell through to "no date at all" and every
    # episode lost its broadcast time.
    #
    # Narrowly gated on purpose. `fromisoformat` also happily parses a bare
    # "2027-03-04" and would hand it back as midnight at `time` precision,
    # which is the invented-time bug this whole function exists to prevent. So
    # it is only tried when the string carries BOTH a time and a zone.
    if "T" in text and (text.endswith("Z") or "+" in text[10:]
                        or "-" in text[11:]):
        try:
            parsed = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None and parsed.tzinfo is not None:
            return int(parsed.timestamp()), "time"
    for fmt, how in _FORMATS:
        try:
            parsed = datetime.datetime.strptime(text, fmt)
        except ValueError:
            continue
        return calendar.timegm(parsed.timetuple()), how
    return None, "year"
