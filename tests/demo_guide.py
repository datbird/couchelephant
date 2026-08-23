"""A guide with enough in it to photograph.

`fake_plex` holds the smallest guide that can prove the app's behaviour: three
programmes, four channels. That is right for tests and wrong for screenshots,
where an almost empty grid says nothing about what the app is for.

So this fills the same fake server with a plausible week of television. It is
invented: no real listing, no real library, nothing from anybody's server. The
screenshots in the README are made from it by `scripts/screenshots.py`, which
means they can be remade whenever the interface changes, and they never show
one person's viewing.

Set COUCHELEPHANT_DEMO_GUIDE=1 before importing fake_plex.
"""

# The call signs are invented. A real lineup names one person's market and
# their tuner, which is not the demo's business.
CHANNELS = {
    "2.1": ("KQAADT", "2.1 KQAADT (FOX)"),
    "4.1": ("WQBBDT", "4.1 WQBBDT (FOX)"),
    "5.1": ("KQCCDT", "5.1 KQCCDT (CBS)"),
    "9.1": ("WQDDDT", "9.1 WQDDDT (ABC)"),
    "19.1": ("KQEEDT", "19.1 KQEEDT (PBS)"),
    "38.1": ("WQFFDT", "38.1 WQFFDT (Independent)"),
    "41.1": ("KQGGDT", "41.1 KQGGDT (NBC)"),
    "62.1": ("WQHHDT", "62.1 WQHHDT (Independent)"),
}

TEAMS = [
    (236, "Kansas City Chiefs"), (237, "Tampa Bay Buccaneers"),
    (238, "Denver Broncos"), (239, "Las Vegas Raiders"),
    (240, "Kansas City Royals"), (241, "Detroit Tigers"),
    (301, "Kansas"), (302, "Kansas State"), (303, "Missouri"),
]

# title, series, genres, rating, minutes, section, teams
SPORT = [
    ("Chiefs at Buccaneers", "NFL Football", ["Football"], None, 180, (236, 237)),
    ("Broncos at Chiefs", "NFL Football", ["Football"], None, 180, (238, 236)),
    ("Raiders at Broncos", "NFL Football", ["Football"], None, 180, (239, 238)),
    ("Royals at Tigers", "MLB Baseball", ["Baseball"], None, 180, (240, 241)),
    ("Kansas at Kansas State", "College Football", ["Football"], None, 210, (301, 302)),
    ("Missouri at Kansas", "College Basketball", ["Basketball"], None, 120, (303, 301)),
]

SHOWS = [
    ("Quarterfinals 1", "The Talent Show", ["Reality"], "TV-PG", 120),
    ("The Long Way Home", "Northern Exposure", ["Drama"], "TV-14", 60),
    ("A Study in Salt", "Coastal Kitchen", ["Cooking"], "TV-G", 30),
    ("Under the Ice", "Deep Field", ["Documentary"], "TV-G", 60),
    ("The Second Door", "Ridgemont", ["Drama", "Mystery"], "TV-14", 60),
    ("Eighteen Across", "Crossword Hour", ["Game Show"], "TV-G", 30),
    ("Sparks", "Workshop", ["Comedy"], "TV-PG", 30),
    ("The Quiet Part", "Ridgemont", ["Drama", "Mystery"], "TV-14", 60),
    ("Harvest", "Coastal Kitchen", ["Cooking"], "TV-G", 30),
    ("Night Shift", "County General", ["Drama"], "TV-14", 60),
    ("The Bell", "Northern Exposure", ["Drama"], "TV-14", 60),
    ("Anything Goes", "Comedy Cellar", ["Comedy"], "TV-MA", 60),
]

MOVIES = [
    ("The Weather in Lisbon", ["Drama", "Romance"], "PG-13", 118),
    ("Cold Harbour", ["Thriller"], "R", 132),
    ("Two Left Feet", ["Comedy"], "PG", 96),
    ("The Cartographer", ["Drama"], "PG-13", 141),
    ("Riverbend", ["Western"], "PG-13", 108),
]


def _key(kind, n):
    return f"plex://episode/demo-{kind}-{n}"


def _rk(guid):
    return guid.replace(":", "%3A").replace("/", "%2F")


# Which programmes each channel runs, in order, on a loop. Sport goes to the
# network affiliates, films to the independents, so the grid reads like a
# lineup rather than like a shuffled list.
# The network affiliates open with a show, so their first game starts at the
# anchor and is still ahead. A game that began an hour ago is past the
# scheduling window, and a pass quite rightly books its repeat instead, which
# photographs like the app getting its one job backwards.
LINEUP = {
    "41.1": ("show", "sport"),
    "5.1": ("show", "sport"),
    "9.1": ("show", "sport"),
    "4.1": ("show", "sport"),
    "2.1": ("show",),
    "19.1": ("show", "movie"),
    "38.1": ("movie", "show"),
    "62.1": ("movie", "show"),
}


def build(anchor, media):
    """(sports, shows, movies, team_tags) for the fake server to serve.

    Each channel is filled back to back from two hours before the anchor to
    fourteen hours after it, so the grid has television in every column. A
    guide with three programmes in it photographs like a broken app.
    """
    start_of_day = anchor - 2 * 3600
    end_of_day = anchor + 14 * 3600

    sports, shows, movies, team_tags = [], [], [], []
    names = dict(TEAMS)
    counters = {"sport": 0, "show": 0, "movie": 0, "news": 0}

    for vcn, rotation in LINEUP.items():
        # The grid opens half an hour before now. A half-hour news slot on
        # every channel fills that first column instead of leaving it blank.
        n = counters["news"]
        counters["news"] += 1
        shows.append({
            "guid": _key("news", n), "ratingKey": _rk(_key("news", n)),
            "type": "episode", "title": "Evening News", "grandparentTitle": "Local News",
            "summary": "The day's news.", "year": 2026, "contentRating": "TV-G",
            "duration": 30 * 60_000, "Genre": [{"tag": "News"}],
            "Media": [media(vcn, start_of_day - 1800, premiere=True, res="1080")],
        })
        clock = start_of_day
        turn = 0
        while clock < end_of_day:
            kind = rotation[turn % len(rotation)]
            turn += 1
            n = counters[kind]
            counters[kind] += 1
            guid = _key(kind, n)
            # Live if it starts near the anchor, which is what the grid opens
            # on; the rest are ordinary listings.
            live = abs(clock - anchor) < 4 * 3600

            if kind == "sport":
                title, series, genres, rating, mins, teams = SPORT[n % len(SPORT)]
                item = {
                    "guid": guid, "ratingKey": _rk(guid), "type": "episode",
                    "title": title, "grandparentTitle": series,
                    "summary": f"{title}. Live coverage.", "year": 2026,
                    "duration": mins * 60_000,
                    "Genre": [{"tag": g} for g in genres],
                    # The live broadcast, and the repeat two days later on a
                    # different channel. That pair is the whole point of the app.
                    "Media": [media(vcn, clock, premiere=True, res="1080"),
                              media("38.1", clock + 2 * 86400)],
                }
                team_tags.append((guid, [{"id": t, "tag": names[t]} for t in teams]))
                sports.append(item)
            elif kind == "show":
                title, series, genres, rating, mins = SHOWS[n % len(SHOWS)]
                shows.append({
                    "guid": guid, "ratingKey": _rk(guid), "type": "episode",
                    "title": title, "grandparentTitle": series,
                    "summary": f"{title}. An episode of {series}.", "year": 2026,
                    "contentRating": rating, "duration": mins * 60_000,
                    "Genre": [{"tag": g} for g in genres],
                    "Media": [media(vcn, clock, premiere=live,
                                    res="1080" if n % 3 else "720")],
                })
            else:
                title, genres, rating, mins = MOVIES[n % len(MOVIES)]
                movies.append({
                    "guid": guid, "ratingKey": _rk(guid), "type": "movie",
                    "title": title, "summary": f"{title}. A film.",
                    "year": 2019 + (n % 5),
                    "contentRating": rating, "duration": mins * 60_000,
                    "Genre": [{"tag": g} for g in genres],
                    "Media": [media(vcn, clock, premiere=False, res="1080")],
                })
            clock += mins * 60

    return sports, shows, movies, dict(team_tags)
