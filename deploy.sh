#!/usr/bin/env bash
# Ship, build and RECREATE the container.
#
# `docker restart` keeps the container pinned to the image it was created with,
# so a rebuilt image is silently ignored. The container must be removed and run
# again for new code to take effect.
set -euo pipefail

HOST="${COUCHELEPHANT_HOST:-your-plex-host}"
REMOTE_DIR="${COUCHELEPHANT_REMOTE_DIR:-/mnt/data/couchelephant}"
PORT="${COUCHELEPHANT_PORT:-8710}"
TZ_NAME="${COUCHELEPHANT_TZ:-America/Chicago}"
TAG="couchelephant:1.0"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "==> packaging"
tar czf /tmp/couchelephant.tgz -C "$HERE" Dockerfile requirements.txt app/
scp -q /tmp/couchelephant.tgz "$HOST:/tmp/couchelephant.tgz"

echo "==> building on $HOST"
ssh -o ConnectTimeout=60 "$HOST" "
  set -e
  rm -rf $REMOTE_DIR/src && mkdir -p $REMOTE_DIR/src $REMOTE_DIR/data
  tar xzf /tmp/couchelephant.tgz -C $REMOTE_DIR/src
  cd $REMOTE_DIR/src
  docker build -t $TAG . 2>&1 | tail -2
"

echo "==> recreating container"
ssh -o ConnectTimeout=60 "$HOST" "
  set -e
  docker rm -f couchelephant >/dev/null 2>&1 || true
  docker run -d --name couchelephant --restart unless-stopped \
    -p $PORT:8710 \
    -v $REMOTE_DIR/data:/data \
    -e TZ=$TZ_NAME \
    $TAG >/dev/null
  sleep 8
  docker ps --filter name=couchelephant --format '    {{.Status}}'
"

echo "==> health"
ssh -o ConnectTimeout=30 "$HOST" "curl -s --max-time 15 http://127.0.0.1:$PORT/healthz"
echo
