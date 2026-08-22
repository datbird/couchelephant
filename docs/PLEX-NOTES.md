# Plex API notes

What a Plex Media Server really returns, as opposed to what the documentation
says. Every item here cost a debugging round, so it is written down.

## Paths

The EPG provider is addressed as `tv.plex.providers.epg.cloud:<dvrKey>`, and
the key comes from `/livetv/dvrs`. It is per server and per DVR, so it is
discovered rather than configured.

Sections have types. Shows and Sports are type 4, Movies is type 1. **Querying
a section with the wrong type returns an empty list rather than an error**,
which is how sixteen channels once came to look blank.

## A bulk listing does not carry teams

`/{provider}/sections/{n}/all` returns `Genre` but **not** `Team`. Team tags
exist only on the per-programme metadata at
`/{provider}/metadata/{ratingKey}`. Sports programmes have to be enriched one
at a time.

Team tags are structured, with stable ids: `{"id": 236, "tag": "Kansas City
Chiefs"}`. `/{provider}/sections/{sports}/team` lists them all and
`?team=<id>` filters. Never string-match a team name.

## The premiere flag

Each `Media` entry on an airing carries `premiere: "1"` for the live broadcast.
The repeat carries no flag. Plex has this information and ignores it when
scheduling.

## Creating a recording

`POST /media/subscriptions?<parameters>` where `parameters` comes verbatim from
the template at `/media/subscriptions/template?guid=<guid>`, **plus**
`targetLibrarySectionID` and `type`. The template parameters alone are a 400.

`prefs[key]=value` appends any setting. `prefs[oneShot]=1` with
`prefs[lineupChannel]` and `prefs[startTimeslot]` pins a recording to one
broadcast.

### The guid is not the ratingKey

`ratingKey` is stored percent-encoded: `plex%3A%2F%2Fepisode%2F...`. Pass that
to an HTTP client that encodes parameters itself and Plex receives it encoded
twice, and answers **400** to every template request. Unquote first.

### The create reply carries the new key

`{"MediaContainer": {"MediaSubscription": [{"key": "55", ...}]}}`. Read it from
there. Hunting for it afterwards in the scheduled list misses, because a
recording more than a day out is not listed there.

### A create can succeed and then be discarded

Plex answers 200, hands back a key, and then drops the subscription on its own,
for instance when the airing is a repeat and the rule is new-airings-only. Read
the subscription back before claiming anything was created.

## Reading recordings

`/media/subscriptions` lists rules. `/media/subscriptions/scheduled` lists
individual grab operations, which is the effective schedule.

`mediaIndex` on a grab operation says **which** airing was chosen, and it comes
back as a string on some payloads and an int on others. Coerce before using it
as an index.

A subscription's target programme is on its body, under `Directory` for a
series rule and `Video` for a single event. The subscription's own `title` is
the template name, "All Episodes", which says nothing about what it follows.

Booleans are inconsistent. `oneShot` comes back as the string `'true'`, not
`'1'`.

## Settings on a template

`enumValues` packs the choices as `value:Label|value:Label`, and the labels are
URL encoded inside that string, so a time reads as `07%3A00 PM` until decoded.

Settings with an empty label are plumbing. Plex hides them in its own dialog
and so should you: `oneShot`, `remoteMedia`, `comskipEnabled`.

## Channels

The only network affiliation Plex exposes is the parenthetical in the channel
title on a guide airing: `41.1 KQGGDT (NBC)`. The tuner's own channel list,
at `/media/grabbers/devices/{id}/channels`, carries key, name, drm and hd, and
no network at all.

## DRM

ATSC 3.0 channels can be flagged DRM. Those airings cannot be recorded by
anyone, so they are excluded from every choice rather than attempted and
failed.
