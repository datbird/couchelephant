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
| `POST /api/pass` | Follow a team straight from the programme panel. `team_id`. Runs the passes at once, so the next game is booked now rather than at the next sync |

`POST /api/record` refuses while preview mode is on, and says so.

## Rules and passes

| | |
| --- | --- |
| `GET /api/rules` | Every rule, ours and Plex's own, with what it follows and where it may record from |
| `GET /api/rules/options` | Plex's own choices for a rule. `kind` (`team`/`series`), `team_id` or `series` |
| `POST /api/rules` | Create one. `kind` is `team`, `series` or `smart`. With no source limit a team or series rule becomes a plain Plex rule instead; a smart one is always ours |
| `POST /api/rules/{id}` | Change a pass: `networks`, `channels`, `enabled`, `settings`, and for a smart pass `filter` and `name` |
| `GET /api/filter/fields` | What a smart filter can ask about: fields, the comparisons each kind accepts, the values in your guide, and how much of it carries a content rating |
| `POST /api/filter/preview` | Count what a filter would record before creating it. `filter` (JSON tree), `networks`, `channels`. Returns the count, a sample, and a warning when it is loose |
| `GET /api/rules/{id}/upcoming` | What that pass will record next, and why it chose each broadcast |
| `POST /api/plexrule/{key}/delete` | Remove one of Plex's own rules |
| `POST /passes/{id}/toggle` | Pause or resume a pass |
| `POST /passes/{id}/delete` | Remove a pass |
| `POST /passes/run` | Run every pass now |

## Pages

| | |
| --- | --- |
| `GET /` | The guide |
| `GET /recordings` | The schedule and the passes. Both are fetched from the API above |
| `GET /settings` | The settings window, on its own page |
| `GET /search` | Guide search results |
| `GET /partial/settings` | Just the settings window, for the gear to open in place |

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

## Backup and restore

See [Your data](DATA.md).

| | |
| --- | --- |
| `GET /api/export` | Download everything you decided, as a zip. `secrets=1` includes the Plex token |
| `POST /api/import/inspect` | What is in an uploaded export, without writing anything. Multipart, field `file` |
| `POST /api/import` | Read an export back in. `file`, `replace`, `secrets` |
| `GET /api/backups/jobs` | Every snapshot job. A stored passphrase is reported as `encrypted`, never returned |
| `POST /api/backups/jobs` | Create or change one. `job_id` to change, `name`, `dest_path`, `every_hours`, `retention`, `passphrase`, `enabled`, `raw_db`, `with_secrets` |
| `POST /api/backups/jobs/{id}/run` | Run it now |
| `POST /api/backups/jobs/{id}/delete` | Remove it |
| `GET /api/backups/archives?dest=` | Archives in a folder, newest first |
| `POST /api/backups/restore` | Put one back. `dest`, `name`, `passphrase`, `replace`. Copies the current state to `before-restore/` first |
| `GET /api/backingstore/config` | Backends, their fields, the current settings and the last run. Passwords are masked |
| `POST /api/backingstore/config` | Save it. A masked password means "leave it alone" |
| `POST /api/backingstore/test` | Connect and touch a store |
| `POST /api/backingstore/run` | Reconcile both ways. `dry_run=1` counts without writing |
| `POST /api/backingstore/restore` | Pull everything down and write nothing back |
| `GET /api/backingstore/status` | What the last run did |
