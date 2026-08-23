# Your data, and getting it back

Most of what CouchElephant holds is a cache. The guide, the channels, the teams
and Plex's own schedule all come from your server and rebuild in seconds. None
of it is worth protecting.

What cannot be rebuilt is what you decided:

| | |
| --- | --- |
| Passes | teams, programmes, smart filters, their source limits and Plex settings |
| Recordings you booked | and which pass booked each one |
| Channel artwork | the images you supplied, and the files themselves |
| Settings | server address, timezone, sync interval, preview mode |
| Accounts | local users, their preferences, and Cloudflare identities |

Three ways to protect it, under **Settings, Backup and restore**. They guard
against different accidents, so having more than one is not redundant.

| | Saves you from |
| --- | --- |
| **Export and import** | moving to another machine, or keeping a copy by hand |
| **Snapshots** | a change you regret |
| **Backing store** | losing the machine |

A live replica does not save you from a mistake. It faithfully copies the
mistake. That is what snapshots are for.

## Export and import

**Settings, Backup and restore, Export and import.** One file, downloaded in
your browser.

It is an ordinary zip, and what is inside is readable JSON on purpose:

```
couchelephant.json      the stores, and what made the file
logos/41.1.png          the channel artwork you supplied
```

A backup only its own program can read is not much of a backup, and being able
to look at what you are about to restore is worth more than a clever format.

**Secrets are left out unless you ask.** The Plex token, the Cloudflare AUD
and the backing-store passwords are credentials, and an export is a file that
ends up in an email. Tick the box if you are moving to a new machine and want
them to come too. Channel artwork travels by file name; the other install puts
it in its own logo folder.

Importing offers two ways in:

- **Merge**, the default. Everything in the file is written; anything else here
  is left alone.
- **Replace.** What the file does not carry is removed, so the result is exactly
  what was exported.

Either way it tells you what is in the file before writing anything.

## Snapshots

**Settings, Backup and restore, Snapshots.** A job says what to keep, where to
put it, how often, and how many to keep.

Each run writes one zip: the export, plus the database files themselves if you
ask. The database copies use SQLite's own online backup, so a snapshot taken
while a sync is writing is a database rather than a torn page.

**Retention only ever removes that job's own archives**, matched on its filename
prefix, so two jobs writing to one folder cannot delete each other's work.

**Encryption is optional and off by default.** Set a passphrase and the zip is
AES-256, which 7-Zip, Keka and WinRAR can all open. The passphrase is stored,
because an unattended run needs it, and never handed back by the API. Type
`off` to remove one.

Restoring reads the archive list from whichever folder you point at, including
one written by an install you no longer have. Before it writes anything it
takes a copy of what is here now, into `before-restore/`.

## Backing store

**Settings, Backup and restore, Database.** Point CouchElephant at another
database and it keeps that database and this one in step, both ways.

| Backend | Needs |
| --- | --- |
| SQLite file | nothing. A path this container can see: a NAS share, a synced folder |
| PostgreSQL | host, database, user, password |
| MySQL or MariaDB | host, database, user, password |

Every backend stores the same shape, one table per store:

```sql
couchelephant_<store>(k TEXT PRIMARY KEY, data TEXT)
```

`data` is the record as JSON. A column per field was tried elsewhere and
collided with reserved column names; a blob dodges that and keeps types.

The local SQLite is always the working store. The external database is a
durable replica, reconciled on demand or on a timer. There is deliberately no
"remote primary" mode: it would be a network hop per query and buy nothing.

### How both directions can be right

A two-way sync that only compares here with there cannot tell a new row from a
deleted one. So each store keeps a **shadow**: what both sides looked like at
the last reconcile, as a hash per record. Comparing against it separates a real
local change from a real remote one.

| Local | Remote | Since the shadow | What happens |
| --- | --- | --- | --- |
| edited | untouched | local only | pushed |
| untouched | edited | remote only | pulled |
| deleted | untouched | local only | deleted there |
| untouched | deleted | remote only | deleted here |
| edited | deleted | both | **the edit wins** |
| edited | edited | both | the later timestamp wins, local on a tie |

An edit beats a delete on purpose. Losing an edit loses work. Losing a delete
costs one more click.

### Restoring is a pull, never a sync

**Settings, Backup and restore, Database, Restore from the store.**

An ordinary reconcile would be wrong here. Restoring onto an empty machine, the
merge reads every missing row as a local delete and erases the very copy you
are restoring from. So a restore writes locally only, then rewrites the shadow
to match what it pulled. The next ordinary reconcile is then a clean no-op,
which is what proves the shadow was rewritten.

### Four rules, each of them a bug somewhere else first

1. **A failed read raises. It never returns a short set.** A merge that reads an
   empty remote against a populated shadow concludes the remote deleted
   everything, and obeys. One transient error is enough to lose the lot.
2. **A key must mean the same row on any machine.** `passes.id` is an
   autoincrement, so passes carry a `uid` and recordings carry `pass_uid`. An
   import relinks them to whatever row numbers this machine happens to use.
3. **Nothing the sync writes is itself synced.** Its own status line used to be
   a durable setting, so every run found one changed record and pushed it,
   for ever.
4. **The guide is never copied.** It is an output. A restore that resurrected a
   stale programme would be worse than one that left it empty.

## What is not covered

Recordings themselves. Those are files in your Plex library, and Plex owns
them. Nothing here reads or writes your media.
