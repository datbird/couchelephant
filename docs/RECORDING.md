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

A pass follows a team, or a programme, and keeps matching new airings on its
own. Every sync it looks at what is coming up, chooses an airing for each, and
books it.

A pass records its decisions whether or not it acts, including the ones it
declined and why. Open a pass to see what it will record next.

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
