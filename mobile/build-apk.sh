#!/usr/bin/env bash
# Builds the debug APK of the Atriolyric Android app:
#   mobile/build-apk.sh            -> mobile/android/app/build/outputs/apk/debug/app-debug.apk
# Needs JDK 21 and an Android SDK with platform 36 (JAVA_HOME / ANDROID_HOME;
# the usual locations are tried when they are not set). See mobile/README.md.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"

if [ -z "${JAVA_HOME:-}" ] || ! "$JAVA_HOME/bin/java" -version 2>&1 | grep -q '"21'; then
  for candidate in /usr/lib/jvm/java-21-openjdk /usr/lib/jvm/java-21-openjdk-amd64 /usr/lib/jvm/temurin-21-jdk; do
    if [ -x "$candidate/bin/java" ]; then export JAVA_HOME="$candidate"; break; fi
  done
fi
"${JAVA_HOME:?JDK 21 not found: set JAVA_HOME}/bin/java" -version 2>&1 | grep -q '"21' || { echo "JAVA_HOME must be a JDK 21" >&2; exit 1; }

if [ -z "${ANDROID_HOME:-}" ]; then
  for candidate in "${ANDROID_SDK_ROOT:-}" "$HOME/.android-sdk" "$HOME/Android/Sdk"; do
    if [ -n "$candidate" ] && [ -d "$candidate/platforms" ]; then export ANDROID_HOME="$candidate"; break; fi
  done
fi
: "${ANDROID_HOME:?Android SDK not found: set ANDROID_HOME}"
export ANDROID_SDK_ROOT="$ANDROID_HOME"

# 1. The bundled page (server set-up), built from web/.
(cd "$here/../web" && { [ -d node_modules ] || npm ci; } && npm run build:shell)
# 2. Copy it into the Android project.
(cd "$here" && { [ -d node_modules ] || npm ci; } && npx cap sync android)
# 3. The APK.
(cd "$here/android" && ./gradlew --no-daemon assembleDebug)
echo "APK: $here/android/app/build/outputs/apk/debug/app-debug.apk"
