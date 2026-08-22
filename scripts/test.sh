#!/usr/bin/env bash
# The whole suite, in a throwaway container.
#
# It never runs in the container that serves the app. A test writes to a
# database and creates recordings, and a suite that inherits a live /data
# destroys the thing it was meant to protect. So: a separate image, a
# container that mounts the source read only and nothing else, and a scratch
# directory inside it that the isolation guard checks before a test runs.
set -euo pipefail

cd "$(dirname "$0")/.."
IMAGE=couchelephant-test

usage() {
  cat <<'USAGE'
scripts/test.sh [options] [pytest args...]

  --unit      only the Python suite, no browser
  --ui        only the browser suite
  --build     rebuild the test image first
  -h, --help  this

Anything else goes to pytest, so `scripts/test.sh --unit -k passes -x` works.
USAGE
}

WHAT=all
BUILD=0
ARGS=()
for a in "$@"; do
  case "$a" in
    --unit)    WHAT=unit ;;
    --ui)      WHAT=ui ;;
    --build)   BUILD=1 ;;
    -h|--help) usage; exit 0 ;;
    *)         ARGS+=("$a") ;;
  esac
done

if ! docker info >/dev/null 2>&1; then
  cat >&2 <<'NODOCKER'
This machine cannot talk to Docker (not installed, not running, or your user is
not in the docker group). The suite runs fine without it:

  python3 -m venv .venv && . .venv/bin/activate
  pip install -r requirements.txt -r requirements-dev.txt
  playwright install chromium
  pytest --ignore=tests/ui     # unit and API
  pytest tests/ui              # browser

The container only exists so the run is identical on every machine.
NODOCKER
  exit 1
fi

if [ "$BUILD" = 1 ] || ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "==> building $IMAGE"
  docker build -f Dockerfile.test -t "$IMAGE" .
fi

# The source is copied out of a read-only mount into the container's own
# scratch, so a test cannot write back over the working tree either.
BOOT='mkdir -p /work/home && cp -r /src/app /src/tests /src/scripts /src/pytest.ini /work/ && cd /work && exec "$@"'

fail=0

if [ "$WHAT" != ui ]; then
  echo "==> unit and API suite"
  docker run --rm --name "ce-tests-unit-$$" \
    -v "$PWD:/src:ro" -w /work \
    --tmpfs /work:exec,size=512m \
    -e HOME=/work/home -e PYTHONPATH=/work \
    "$IMAGE" bash -c "$BOOT" -- \
      pytest --ignore=tests/ui "${ARGS[@]}" || fail=1
fi

if [ "$WHAT" != unit ]; then
  echo "==> browser suite"
  # The fake guide is anchored just ahead of now, because the grid draws around
  # the current time and a guide dated 2096 would be an empty screen.
  anchor=$(( $(date +%s) / 1800 * 1800 + 1800 ))
  docker run --rm --name "ce-tests-ui-$$" \
    -v "$PWD:/src:ro" -w /work \
    --tmpfs /work:exec,size=512m \
    --ipc=host \
    -e HOME=/work/home -e PYTHONPATH=/work \
    -e COUCHELEPHANT_FAKE_ANCHOR="$anchor" \
    "$IMAGE" bash -c "$BOOT" -- \
      pytest tests/ui "${ARGS[@]}" || fail=1
fi

if [ "$fail" = 0 ]; then
  echo "==> clean"
else
  echo "==> FAILURES above"
fi
exit "$fail"
