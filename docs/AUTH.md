# Accounts

Three modes, under **Settings, Accounts**. A fresh install uses the first.

| Mode | Who decides | Right for |
| --- | --- | --- |
| **Off** | nobody | A box on your own network |
| **Local** | CouchElephant | A handful of people, no other infrastructure |
| **Cloudflare Access** | Cloudflare | Anything reachable from the internet |

## Off

No sign-in. Anyone who can reach the page can use it, including changing
settings and creating recordings.

This is the default because a new install has nothing worth protecting yet and
a forced account creation is a wall in front of a thing you have not decided to
keep. It is the wrong setting the moment this is reachable from anywhere but
your own network.

## Local accounts

Turn it on and the next visitor is asked to create the first account, which is
the administrator. After that, `/login`.

Passwords are scrypt hashed with a per-user random salt. Session tokens are
random, stored hashed, and last 30 days. A copy of `auth.db` grants no logins.

A username that does not exist is still hashed against before failing, so a
wrong username cannot answer measurably faster than a wrong password.

## Cloudflare Access

Put CouchElephant behind a Cloudflare Access application and let Cloudflare
authenticate. You need two things from the Access dashboard:

- **Team domain**, for example `yourteam.cloudflareaccess.com`
- **AUD**, the application's Audience tag

Cloudflare forwards each request with a signed token in
`Cf-Access-Jwt-Assertion`, mirrored in the `CF_Authorization` cookie.
CouchElephant verifies that token against your team's public keys and the AUD.

**The plain email header is never trusted on its own.** A request that reaches
the origin without passing through Cloudflare cannot claim an identity, which
is the point of verifying rather than reading.

An email Cloudflare has authenticated but that has no local account gets one on
first sight. Cloudflare has already decided who may reach the app; asking you
to add each person again after that would be theatre.

### It refuses to lock you out

Switching to Cloudflare mode fetches your team's signing keys first and refuses
the change if it cannot reach them. A typo in the team domain would otherwise
lock everyone out of a box that may not be reachable another way.

For the same reason, turning sign-in **off** is always allowed from inside. If
Access breaks, reach the box on your own network and switch back.

## Per-account preferences

Dark mode belongs to the person, not the server. Signed in, the choice is
stored against the account and stamped into the page by the server, so it
follows you to another browser and cannot flash the previous account's choice
on the way in. Signed out, the browser keeps it, which is the only place it can
live when nobody is identified.
