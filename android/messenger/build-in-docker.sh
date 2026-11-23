#!/usr/bin/env bash
# Rebuilds app/devices/gremote-messenger.apk (committed, so every install gets it on `update`).
# Bump android:versionCode in AndroidManifest.xml whenever the app changes, so TVs upgrade.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
docker build --platform linux/amd64 -t gremote-apk-builder "$HERE"
docker run --rm --platform linux/amd64 -v "$HERE:/src:ro" -v "$ROOT/app/devices:/out" \
  gremote-apk-builder bash /src/build.sh /sdk /out/gremote-messenger.apk
