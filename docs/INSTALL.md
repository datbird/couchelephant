# Install and configure

## What you need

- A Plex Media Server with a DVR set up and a channel lineup.
- Docker on any machine that can reach that server.

CouchElephant does not need to run on the same machine as Plex, and it does not
touch your media files.

## Run it

```bash
docker run -d --name couchelephant --restart unless-stopped \
  -p 8710:8710 \
  -v /opt/couchelephant/data:/data \
  -e TZ=UTC \
  ghcr.io/datbird/couchelephant:latest
```

The image is built for `linux/amd64` and `linux/arm64`, so the same tag works
on an ordinary server and on a Pi. Pin a version if you would rather choose
when to move: `:1` follows fixes within a major version, `:1.2.3` never moves.

There is a `docker-compose.yml` in the repository that does the same thing.

### On Unraid

Search for **CouchElephant** in **Apps**. The template sets the port to 8710
and the appdata path to `/mnt/user/appdata/couchelephant`, which is all it
needs. Everything else is configured in the app itself.

### From source

Clone the repository and run `docker build -t couchelephant .`, then use
`couchelephant` in place of the image name above.

Everything it keeps lives in the one volume, so that is the only thing to back
up.

| Path in the volume | What it holds |
| --- | --- |
| `couchelephant.db` | The guide cache, rules, and the decision log |
| `auth.db` | Accounts and sessions, kept apart so rebuilding the cache cannot take them |
| `logos/` | Cached channel logos, and any you supplied yourself |

## Environment

| Variable | Default | What it does |
| --- | --- | --- |
| `COUCHELEPHANT_DB` | `/data/couchelephant.db` | The main database |
| `COUCHELEPHANT_AUTH_DB` | next to the main one | Accounts and sessions |
| `COUCHELEPHANT_LOGOS` | `/data/logos` | Cached channel artwork |
| `TZ` | `UTC` | The container's clock |

Everything else is a setting in the app, not an environment variable, because
it is the kind of thing you change while looking at the result.

## Point it at Plex

Open `http://your-host:8710` and go to **Settings, Plex**.

**Address.** The address must work from inside the container. `127.0.0.1` only
works if Plex is running in that same container, which it is not, so use the
machine's real address.

**Token.** On the Plex server this is the `PlexOnlineToken` in
`Preferences.xml`. It is kept in the local database and never written to a log.
The field shows dots once one is stored; leave them alone to keep the current
token.

Press **Test connection**. It checks three separate things and tells you which
one failed:

| What it says | What to do |
| --- | --- |
| No server address set | Fill in the address |
| No token set | Paste the token |
| Could not reach *address* | Wrong host, wrong port, or nothing listening. Remember it must resolve from inside the container |
| The server answered but rejected the token | The token is wrong or expired |
| Reached it, but it has no DVR | Add a tuner to Plex first |

## Timezone

Under **Settings, Plex, Guide**. Every time in the app is shown in this zone.
The guide itself is stored as UTC timestamps, so changing this never moves a
recording.

## Preview mode

On for a new install, under **Settings, Recording**.

With it on, rules work out which airing they would choose and show it to you,
but nothing is written to your DVR. It exists so you can watch CouchElephant
make its choices before you trust it with your server. Turn it off when the
choices look right.

## Sign-in

Off for a new install. See [AUTH.md](AUTH.md).

Turn it on before this is reachable from anywhere but your own network.

## Sync

The guide refreshes on a timer, default 60 minutes, set under **Settings, Plex,
Guide**. Each sync pulls channels, programmes, airings, teams and Plex's own
recordings, then re-runs every pass. The sync icon in the header runs one now,
and its tooltip carries the last result.
