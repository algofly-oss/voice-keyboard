#!/bin/sh
# Publishes a release: builds the desktop client for macOS, Linux and Windows
# (x86-64 and ARM64) and the ESP32 / ESP32-C3 firmware (in Docker) locally,
# tags the current commit, and creates the GitHub release with the binaries
# attached. The backend mirrors the newest release.
# Usage: tools/release.sh 1.0.0
set -eu
version=${1:?usage: $0 <version, e.g. 1.0.0>}
tag="v$version"
cd "$(dirname "$0")/.."

[ -z "$(git status --porcelain -- desktop esp32)" ] || { echo "Commit desktop/ and esp32/ changes first" >&2; exit 1; }
git fetch -q origin
[ "$(git rev-parse HEAD)" = "$(git rev-parse '@{u}')" ] || { echo "Push your branch first" >&2; exit 1; }

out=$(mktemp -d)
# Pure-Go builds (no cgo), so all six cross-compile from one machine.
(cd desktop && for target in linux/amd64 linux/arm64 darwin/amd64 darwin/arm64 windows/amd64 windows/arm64; do
  goos=${target%/*}; goarch=${target#*/}; ext=""; ldflags="-s -w -X main.version=$version"
  [ "$goos" = windows ] && ext=.exe && ldflags="$ldflags -H windowsgui"
  CGO_ENABLED=0 GOOS=$goos GOARCH=$goarch go build -trimpath -ldflags "$ldflags" -o "$out/vkeyboard-$goos-$goarch$ext" .
done)
# macOS: our own certificate, so an updated client keeps its Accessibility permission.
tools/macos-sign.sh "$out/vkeyboard-darwin-amd64" "$out/vkeyboard-darwin-arm64"
esp32/build.sh "$version"
cp esp32/dist/vkeyboard-esp32.bin esp32/dist/vkeyboard-esp32c3.bin "$out"/

git tag -a "$tag" -m "Voice Keyboard $version"
# Push with the gh login: a plain git push may pick up an older credential
# (e.g. ~/.netrc) without the scopes this repository needs.
git push -q "https://x-access-token:$(gh auth token)@github.com/$(gh repo view --json nameWithOwner -q .nameWithOwner).git" "$tag"
gh release create "$tag" "$out"/* --verify-tag --title "Voice Keyboard $version" \
  --notes "Desktop client builds, installed by the command in the web app under Settings → Clients; ESP32 firmware, installed from the web app's /esp32 page."
echo "Released $tag. The backend picks it up within RELEASE_SYNC_SECONDS (or on restart)."
