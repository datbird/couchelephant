"""The guard that makes this suite safe to run.

A test suite for this app has to write to a database and create recordings.
Both of those, pointed at the wrong place, destroy something real: the live
guide cache, or somebody's DVR.

So nothing here trusts the tests. Every module calls `assert_isolated()` before
it touches anything, and that refuses to run unless the paths in the
environment are demonstrably scratch. A test that forgets to set them up does
not quietly inherit the container's `/data`; it fails.

This is not theoretical. The same pattern in the same author's ludodex, without
this guard, wiped 66,000 rows from a live index because one test used
`os.environ.setdefault` and inherited the real data directory.
"""
import os
import sys

# Anything under these is production, whatever else the environment says.
FORBIDDEN_PREFIXES = ("/data", "/config", "/mnt", "/var/lib")
# A scratch path has to say so.
REQUIRED_MARKERS = ("ce-test", "pytest", "tmp")


class NotIsolated(RuntimeError):
    pass


def _check_path(name, value):
    if not value:
        raise NotIsolated(f"{name} is not set. The suite refuses to guess.")
    real = os.path.realpath(value)
    for bad in FORBIDDEN_PREFIXES:
        if real == bad or real.startswith(bad + os.sep):
            raise NotIsolated(
                f"{name}={real} is under {bad}, which is production. Refusing.")
    if not any(m in real for m in REQUIRED_MARKERS):
        raise NotIsolated(
            f"{name}={real} does not look like scratch. It must contain one of "
            f"{REQUIRED_MARKERS}.")
    return real


def assert_isolated():
    """Raise unless every path this app writes to is scratch."""
    _check_path("COUCHELEPHANT_DB", os.environ.get("COUCHELEPHANT_DB"))
    _check_path("COUCHELEPHANT_AUTH_DB", os.environ.get("COUCHELEPHANT_AUTH_DB"))
    _check_path("COUCHELEPHANT_LOGOS", os.environ.get("COUCHELEPHANT_LOGOS"))
    # A test must never reach a real Plex. The fake one binds to localhost.
    url = os.environ.get("COUCHELEPHANT_TEST_PLEX", "")
    if url and not (url.startswith("http://127.0.0.1") or
                    url.startswith("http://localhost")):
        raise NotIsolated(f"COUCHELEPHANT_TEST_PLEX={url} is not local. Refusing.")
    return True


if __name__ == "__main__":
    try:
        assert_isolated()
    except NotIsolated as e:
        print(f"NOT ISOLATED: {e}", file=sys.stderr)
        raise SystemExit(1)
    print("isolated")
