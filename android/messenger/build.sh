#!/usr/bin/env bash
# Builds gremote-messenger.apk without Gradle: aapt2 (manifest) -> javac -> d8 -> sign.
# Usage: build.sh <android sdk dir> <output apk>   (run by the Dockerfile's apk stage)
set -euo pipefail

SDK="$1"
OUT="$2"
HERE="$(cd "$(dirname "$0")" && pwd)"
BT="$(ls -d "$SDK"/build-tools/* | sort -V | tail -1)"
JAR="$(ls -d "$SDK"/platforms/android-*/android.jar | sort -V | tail -1)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

"$BT/aapt2" link -o "$WORK/base.apk" -I "$JAR" --manifest "$HERE/AndroidManifest.xml"

mkdir -p "$WORK/classes" "$WORK/dex"
javac --release 8 -Xlint:-options -classpath "$JAR" \
  -d "$WORK/classes" $(find "$HERE/src" -name '*.java')
"$BT/d8" --release --min-api 26 --lib "$JAR" --output "$WORK/dex" \
  $(find "$WORK/classes" -name '*.class')

cp "$WORK/base.apk" "$WORK/unsigned.apk"
(cd "$WORK/dex" && zip -q -j "$WORK/unsigned.apk" classes.dex)
"$BT/zipalign" -f 4 "$WORK/unsigned.apk" "$WORK/aligned.apk"

# A fresh key per build is fine: gremote uninstalls and reinstalls if the TV has an
# older copy signed with a different key.
keytool -genkeypair -keystore "$WORK/key.jks" -storepass gremote -keypass gremote \
  -alias gremote -keyalg RSA -keysize 2048 -validity 10000 \
  -dname "CN=gremote" >/dev/null 2>&1
mkdir -p "$(dirname "$OUT")"
"$BT/apksigner" sign --ks "$WORK/key.jks" --ks-pass pass:gremote --key-pass pass:gremote \
  --out "$OUT" "$WORK/aligned.apk"
rm -f "$OUT.idsig"
echo "Built $OUT"
