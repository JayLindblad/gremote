#!/usr/bin/env bash
# Rebuilds app/devices/gremote-messenger.apk (committed, so every install gets it on `update`).
# Each build raises android:versionCode by one and writes it to gremote-messenger.version,
# which gremote reads: TVs with an older copy get the new one with their next message.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
MANIFEST="$HERE/AndroidManifest.xml"

OLD="$(perl -ne 'print $1 if /android:versionCode="(\d+)"/' "$MANIFEST")"
[[ -n "$OLD" ]] || { echo "No android:versionCode in $MANIFEST" >&2; exit 1; }
NEW=$((OLD + 1))

docker build --platform linux/amd64 -t gremote-apk-builder "$HERE"
perl -pi -e "s/android:versionCode=\"\\d+\"/android:versionCode=\"$NEW\"/; \
             s/android:versionName=\"[^\"]*\"/android:versionName=\"$NEW\"/" "$MANIFEST"
if ! docker run --rm --platform linux/amd64 -v "$HERE:/src:ro" -v "$ROOT/app/devices:/out" \
    gremote-apk-builder bash /src/build.sh /sdk /out/gremote-messenger.apk; then
  # Failed build: put the old number back so the manifest matches the committed APK.
  perl -pi -e "s/android:versionCode=\"\\d+\"/android:versionCode=\"$OLD\"/; \
               s/android:versionName=\"[^\"]*\"/android:versionName=\"$OLD\"/" "$MANIFEST"
  exit 1
fi
echo "$NEW" > "$ROOT/app/devices/gremote-messenger.version"
echo "Messenger version $OLD -> $NEW. Commit the manifest, the .apk and the .version file."
