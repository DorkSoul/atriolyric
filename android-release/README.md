# The Android app's APK for the image

The image build copies this folder to `/app/android`. When it holds
`atriolyric.apk` (and `atriolyric.apk.json` naming its version), the server
offers the app for download (Settings > About) and installed apps can update
from it. The APK is never committed: just before it builds the image, the
GitHub workflow downloads the `atriolyric.apk` attached to the GitHub release
(`mobile/release-apk.sh --release`), and an image built without one simply
offers no app. See [mobile/README.md](../mobile/README.md) and
[docs/releasing.md](../docs/releasing.md).
