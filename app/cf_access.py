"""Cloudflare Access: verify the token it sends, and read the email from it.

Behind a Cloudflare Access application, Cloudflare authenticates the person and
forwards each request with a signed token in `Cf-Access-Jwt-Assertion`, also
mirrored in the `CF_Authorization` cookie.

The token is verified against the team's public keys and the application's AUD
tag. The plain email header alone is never trusted, so a request that reaches
the origin without passing through Cloudflare cannot claim an identity.
"""
import json
import urllib.request

try:
    import jwt
    from jwt import PyJWKClient
except ImportError:                      # pragma: no cover
    jwt = None
    PyJWKClient = None

_clients = {}


def available() -> bool:
    return jwt is not None


def _client(team_domain):
    url = f"https://{team_domain.strip().strip('/')}/cdn-cgi/access/certs"
    c = _clients.get(url)
    if c is None:
        c = PyJWKClient(url, cache_keys=True)
        _clients[url] = c
    return c


def verify_email(token: str | None, team_domain: str | None, aud: str | None) -> str | None:
    """The verified email, or None if anything about the token does not hold."""
    if not token or not team_domain or not aud or not available():
        return None
    team_domain = team_domain.strip().strip("/")
    try:
        key = _client(team_domain).get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token, key.key, algorithms=["RS256"],
            audience=aud, issuer=f"https://{team_domain}",
            options={"require": ["exp", "iss", "aud"]},
        )
    except Exception:
        return None
    email = (claims.get("email") or claims.get("identity") or "").strip().lower()
    return email or None


def check(team_domain: str | None, aud: str | None) -> tuple[bool, str]:
    """Can this app reach the team's certs? Used by the Settings test button."""
    if not available():
        return False, "PyJWT is not installed in this image"
    if not team_domain or not aud:
        return False, "team domain and AUD are both required"
    url = f"https://{team_domain.strip().strip('/')}/cdn-cgi/access/certs"
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            keys = json.load(r).get("keys") or []
    except Exception as e:
        return False, f"could not fetch {url}: {type(e).__name__}: {e}"
    if not keys:
        return False, "that team domain returned no signing keys"
    return True, f"reached {team_domain}, {len(keys)} signing key(s)"
