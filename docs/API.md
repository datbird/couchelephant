# HTTP API

Everything the browser does, it does through these. There is no separate
public API and no versioning: this is one app talking to itself, written down
so you can drive it from a script if you want to.

With sign-in on, every path except the sign-in screens and `/healthz` needs a
session. An unauthenticated call to anything under `/api/` gets `401` with a
JSON body rather than a redirect.

## Guide

| | |
| --- | --- |
| `GET /api/grid` | A window of the guide. `start`, `end`, `choffset`, `chlimit`. Returns channels and airings for that window |
| `GET /api/program?airing_id=` | One programme: every airing of it, which is live, the teams, and **why** it is being recorded if it is |
| `GET /api/facets` | Counts for the filter panel: kinds, channels, genres, teams |
| `GET /partial/airings` | Search result rows, for infinite scroll |
| `GET /logo/{vcn}` | A channel logo. Yours if you supplied one, otherwise the guide's, otherwise a blank |

## Recording

| | |
| --- | --- |
| `GET /api/record/options?airing_id=` | What Plex offers for this programme, read from its own template, plus the networks and channels a rule could be limited to |
| `POST /api/record` | Schedule a broadcast. `airing_id`, `template`, `settings` (JSON), `networks` (JSON), `channels` (JSON) |
| `POST /api/record/cancel` | Cancel something we booked. `airing_id` |

`POST /api/record` refuses while preview mode is on, and says so.

## Rules and passes

| | |
| --- | --- |
| `GET /api/rules` | Every rule, ours and Plex's own, with what it follows and where it may record from |
| `GET /api/rules/options` | Plex's own choices for a rule. `kind` (`team`/`series`), `team_id` or `series` |
| `POST /api/rules` | Create one. With no source limit this books a plain Plex rule instead |
| `POST /api/rules/{id}` | Change a pass: `networks`, `channels`, `enabled` |
| `GET /api/rules/{id}/upcoming` | What that pass will record next, and why it chose each broadcast |
| `POST /api/plexrule/{key}/delete` | Remove one of Plex's own rules |
| `POST /passes/{id}/toggle` | Pause or resume a pass |
| `POST /passes/{id}/delete` | Remove a pass |
| `POST /passes/run` | Run every pass now |

## Schedule

| | |
| --- | --- |
| `GET /api/schedule` | The effective schedule on the Plex server. `offset`, `limit`, `start`, `end`. Every row says who booked it and why |
| `GET /api/teams?q=` | Teams appearing in the guide |
| `GET /api/series?q=` | Programmes still to air, grouped by show |
| `GET /api/sources` | Networks and channels a rule can be limited to |

## Settings

| | |
| --- | --- |
| `POST /settings` | Server address, token, timezone, sync interval, preview mode |
| `POST /settings/test` | Check the Plex connection. Send `Accept: application/json` for `{ok, detail}` |
| `POST /settings/auth` | Sign-in mode and the Cloudflare fields |
| `POST /settings/logos` | Re-fetch every channel logo |
| `POST /settings/channels/{vcn}/logo` | Upload your own logo for a channel. Multipart, field `logo` |
| `POST /settings/channels/{vcn}/logo/reset` | Go back to the guide's logo |
| `POST /settings/users/{id}/delete` | Remove an account |
| `POST /api/theme` | Remember the theme against the account. `theme` is `light` or `dark` |
| `POST /sync` | Run a guide sync now |

## Sign-in

| | |
| --- | --- |
| `GET`/`POST` `/welcome` | First run. Tests the Plex connection before saving anything |
| `GET`/`POST` `/setup` | Create the first account, when sign-in has just been turned on |
| `GET`/`POST` `/login` | Sign in |
| `POST /logout` | Sign out |

## Health

`GET /healthz` needs no sign-in and returns whether it is configured, whether
preview mode is on, the last sync, and how many airings and passes it holds.
Useful as a container health check.

## Shapes

Everything under `/api/` answers JSON. Success is `{"ok": true, ...}`. Failure
is `{"ok": false, "error": "..."}` with a matching status code, and the error
is a sentence meant for a person, not a code.
