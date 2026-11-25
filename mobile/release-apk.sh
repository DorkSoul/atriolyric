#!/usr/bin/env bash
# Builds the signed release APK, and with --release publishes the GitHub
# release for this version with the APK attached as atriolyric.apk:
#   mobile/release-apk.sh            -> mobile/dist/atriolyric-<version>.apk
#   mobile/release-apk.sh --release  also publishes release v<version> (gh CLI);
#                                    the tag must already be pushed
#   --allow-dirty                    build although the working tree has
#                                    uncommitted changes (not for a release)
# The version is web/package.json's. Publishing the release starts the image
# build, which puts this APK inside the image.
#
# The signing key is read from ~/.config/atriolyric/android-signing.env
# (ATRIO_KEYSTORE, ATRIO_KEYSTORE_PASSWORD, ATRIO_KEY_ALIAS). Keep a backup of
# that keystore: Android only updates an app signed with the same key.
#
# The passwords reach Gradle only. npm, Vite and Capacitor run first, without
# them: a dependency's install or build script never sees the key's password.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
release=0
allow_dirty=0
for arg in "$@"; do
  case "$arg" in
    --release) release=1 ;;
    --allow-dirty) allow_dirty=1 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

# A published APK must be built from committed code, so it matches its tag.
if [ "$allow_dirty" = 0 ] && [ -n "$(git -C "$here/.." status --porcelain --untracked-files=no)" ]; then
  echo "The working tree has uncommitted changes: commit them first (or pass --allow-dirty for a test build)." >&2
  exit 1
fi
if [ "$release" = 1 ] && [ "$allow_dirty" = 1 ]; then
  echo "--release needs a clean working tree; --allow-dirty is for local test builds only." >&2
  exit 2
fi

signing="${ATRIO_SIGNING_ENV:-$HOME/.config/atriolyric/android-signing.env}"
[ -f "$signing" ] || { echo "No signing settings at $signing" >&2; exit 1; }

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

version=$(node -p "require('$here/../web/package.json').version")
(cd "$here/../web" && { [ -d node_modules ] || npm ci; } && npm run build:shell)
(cd "$here" && { [ -d node_modules ] || npm ci; } && npx cap sync android)

# Only now the signing settings, and only in Gradle's environment.
(
  set -a
  # shellcheck source=/dev/null
  . "$signing"
  set +a
  [ -f "${ATRIO_KEYSTORE:?ATRIO_KEYSTORE is not set}" ] || { echo "No keystore at $ATRIO_KEYSTORE" >&2; exit 1; }
  cd "$here/android" && ./gradlew --no-daemon assembleRelease
)

mkdir -p "$here/dist"
apk="$here/dist/atriolyric-$version.apk"
cp "$here/android/app/build/outputs/apk/release/app-release.apk" "$apk"
# Refuse to hand out anything that is not signed with the release key.
apksigner=$(ls "$ANDROID_HOME"/build-tools/*/apksigner | sort -V | tail -1)
"$apksigner" verify --print-certs "$apk" | grep -E "Signer #1 certificate (DN|SHA-256)"
echo "APK: $apk ($(stat -c %s "$apk") bytes, version $version)"

if [ "$release" = 1 ]; then
  tag="v$version"
  git -C "$here" ls-remote --exit-code --tags origin "refs/tags/$tag" >/dev/null \
    || { echo "Tag $tag is not on origin: push it first." >&2; exit 1; }
  asset_dir=$(mktemp -d)
  trap 'rm -rf "$asset_dir"' EXIT
  cp "$apk" "$asset_dir/atriolyric.apk"
  if gh release view "$tag" >/dev/null 2>&1; then
    gh release upload "$tag" "$asset_dir/atriolyric.apk" --clobber
    echo "Replaced atriolyric.apk on release $tag. Re-run the image build to pick it up."
  else
    gh release create "$tag" "$asset_dir/atriolyric.apk" --verify-tag \
      --title "Atriolyric $version" --generate-notes
    echo "Published release $tag with atriolyric.apk."
  fi
fi
