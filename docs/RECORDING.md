# How recording works

## The problem

The same game appears in the guide several times: live on one channel, then as
a repeat on another, often days later. Plex's guide marks the live one
`premiere: 1`. Plex ignores that and breaks the tie on the lowest channel
number, so a team pass records the repeat.

Nothing tells you. You find out when you sit down to watch a game that already
finished.

## Choosing the airing

CouchElephant groups every airing of the same programme, then picks one:

1. Drop anything DRM encrypted. It cannot be recorded, so it is not a candidate.
2. Of what is left, prefer the airings flagged as a premiere.
3. Among those, take the earliest.
4. With no premiere flag anywhere, take the earliest usable airing and say so.

The reason is kept with the choice, in those words, and shown in the app. If
every airing is encrypted it says that rather than silently doing nothing.

## Pinning it

Picking is not enough. Handing Plex a normal recording lets Plex choose again,
and it chooses wrong. So CouchElephant creates a **one-shot** subscription with
two settings that leave nothing open:

| Setting | Value |
| --- | --- |
| `oneShot` | `1` |
| `lineupChannel` | the channel of the airing it chose |
| `startTimeslot` | the start time of that airing |

Plex now has exactly one broadcast it can record.

## Passes

A pass follows something and keeps matching new airings on its own. Every sync
it looks at what is coming up, chooses an airing for each, and books it.

There are three kinds:

| | |
| --- | --- |
| **Sports team** | Every game the team plays, always the live broadcast |
| **Smart filter** | Anything in the guide matching a tree of conditions |
| **Programme or series** | Every episode the guide carries |

The first two are offered together under **Smart Pass**, because they are the
two ways of saying "record things I have not named individually".

A pass records its decisions whether or not it acts, including the ones it
declined and why. Open a pass to see what it will record next.

### Which teams you can follow

Plex only knows the teams playing inside the guide it holds, about eleven days.
On a real server that was **76 teams**: whoever happens to be on this week. You
could not follow your team in the off-season, and a college side whose season
starts in November was simply absent in August.

So CouchElephant ships its own catalogue, `app/data/teams.json`, and the list
you pick from is the union of that and whatever Plex currently knows.

| League | Teams |
| --- | --- |
| NFL, NBA, MLB, NHL, WNBA, MLS, NWSL | every side |
| NCAA | 929 schools, across football, both basketballs, baseball and hockey |
| Premier League, La Liga, Serie A, Bundesliga, Ligue 1, Liga MX, and their second tiers | every side |

1,310 in total. Rebuild it with `python3 scripts/build_teams.py`.

**The catalogue is how you find a team, not what makes a pass work.** An airing
carries Plex's own team ids, so a pass follows an id. A team Plex has seen has
one and starts at once. A team only in the catalogue has none yet, so the pass
says **waiting for this team to appear in the guide** rather than looking like
it is running. The next sync that carries the team fills the id in and the pass
starts booking.

Names are matched on a normalised form, so accents, club words and punctuation
do not split one team into two: `Club Tijuana` is `Tijuana`, `FC Bayern
Munchen` is `Bayern Munich`, `San Jose State` is `San José State`. The same
rule is written in the app and in the builder, and a test compares them, since
two spellings of the rule means nothing ever matches.

Teams are also no longer deleted when they stop playing. They keep their row,
their id and their name, and lose only the "in the guide" mark. The list grows
over a season instead of shrinking to this week.

### Smart filters

A smart filter is a nested tree. A group matches **all**, **any** or **none** of
what is in it, and a group can hold more groups, to any depth. So "comedy or
sci-fi, not rated TV-MA, first shown after 2015" is one rule.

What can be asked, all of it from Plex's own guide:

| | |
| --- | --- |
| The programme | title, series, description, genre, content rating, year, kind, first shown, length |
| The broadcast | channel, network, high definition, live, day of the week, start hour |

There is no quality score. Plex's guide carries none: not a star rating, not an
audience score, not a critic score. Content rating means the parental one,
TV-14 or PG-13.

**A blank is a decision, not a default.** The guide does not rate everything. On
one real server 294 of 400 shows carried a content rating and 40 of 159 sports
did. In SQL a missing value fails every comparison including a negative one, so
"content rating is not TV-MA" would quietly let every unrated programme
through. Each condition therefore carries **or blank**, off to start with, and
the panel says how much of your guide can answer at all.

**A smart filter is always CouchElephant's.** A Plex rule follows one programme
or one team. It cannot be given conditions, so there is no Plex form to hand
this to and no decision about who owns it.

**Loose filters are questioned.** The panel counts what a filter would record
before anything is created, and shows the first matches. A filter that would
book more than 40 programmes, or that narrows only by where and when something
airs rather than by what it is, has to be confirmed a second time. The button
says the number. It is a question, not a refusal: press again and it is made.

### Plex's own settings on a pass

Every kind of pass, including a smart filter, is offered Plex's own recording
settings: padding before and after, resolution, whether to replace a lower
quality copy, whether to allow a partial airing, commercial detection.
CouchElephant applies them to every airing that pass books.

**They are the one-shot template's settings, not the recurring one's.** A pass
books a pinned one-shot for each airing it matches, so the three choices only a
recurring rule can honour are not offered: whether to take repeats, and the two
policies about deleting episodes it has kept. Offering a control that is
dropped on save is worse than not offering it.

The channel and the airing time are not offered either. CouchElephant sets
those itself, per airing. That pin is the mechanism this whole app exists for.

Each setting carries **Plex's own explanation of it**, taken from the template
rather than rewritten here, so there is only one version of those words.

**Sport overruns, so a sports pass arrives with padding filled in**: one minute
before and sixty after, on screen, before you create it. Without it a game that
runs long is cut off at the time the guide claimed it would end, which it very
often does not.

Plex sends the two padding fields as a plain integer with no list of allowed
values, so **there is no ceiling**. The field suggests 0, 5, 15, 30, 45, 60, 90,
120 and 180 minutes after the end, and still accepts anything you type.

### Source limits

A pass can be limited to a set of networks, a set of channels, or both. The two
lists are one allowlist rather than two filters that both have to pass: naming
a network and a channel means either of them, which is what a person means by
"only ABC, CBS and channel 41.1".

The limit is applied **before** the airing is chosen, not after, so a pass
picks the best broadcast among the ones it may use rather than picking first
and then finding it disallowed. When nothing qualifies it says so, by name:
`no airing is on ABC or CBS`.

Networks come from Plex's own guide. It names a channel `41.1 KQGGDT (NBC)`,
and that trailing parenthetical is the only affiliation Plex exposes; the
tuner's channel list carries none. The name is used as it appears, without
aliasing, so the picker shows exactly what your guide says.

## Who owns a rule

Two systems can hold a recording rule, and the app always says which one will.

| | CouchElephant | Plex |
| --- | --- | --- |
| Colour | amber | blue-grey |
| Holds the rule | CouchElephant | Plex |
| Chooses each airing | CouchElephant, every sync | Plex |
| Can limit to several networks | yes | no |
| Always takes the live broadcast | yes | no |

The rule is simple. If nothing you set needs CouchElephant, it becomes an
ordinary Plex rule and Plex takes it from there. Set a source limit on a
recurring choice and Plex cannot express it, so CouchElephant keeps the rule and
books each airing itself.

A bar across the top of the record panel says which is about to happen, and it
updates as you change the options.

## Plex's own options

The options in the panel are read from Plex's recording template for that
programme, not copied into this app. Whatever Plex offers appears, with Plex's
own labels: resolution, padding before and after, partial airings, commercial
detection, how many to keep, and so on. Settings Plex leaves unlabelled are
plumbing and stay hidden, the same as in its own dialog.

A pass carries those settings too and applies them to every broadcast it books,
so using a pass does not mean giving up padding or quality. The pin to channel
and time stays CouchElephant's, because that is the whole mechanism.

## Cancelling

Anything CouchElephant booked can be cancelled from the same panel. It deletes
the subscription in Plex and forgets the airing. If the recording had already
started, it says so, because Plex keeps the part it captured.

## What "already handled" means

Before booking, a pass checks whether the game is covered. It skips when:

- a pass already scheduled this programme, or
- Plex already has a recording of it, scheduled, running or finished.

That is why an entry can read `already scheduled by a pass` rather than
`will schedule`.
