#!/bin/sh
# Downloads the latest Voice Keyboard mobile builds from GitHub Releases into
# mobile/releases/, which the backend serves at /downloads. The repository is
# private, so phones cannot fetch release assets directly; the server does.
# Requires the GitHub CLI (gh auth login). Optional: tools/fetch_mobile_release.sh <tag>
set -eu
cd "$(dirname "$0")/.."
tag=${1:-$(gh release list --limit 50 --json tagName --jq '[.[].tagName | select(startswith("voice-keyboard-v"))][0]')}
[ -n "$tag" ] || { echo "No voice-keyboard release found" >&2; exit 1; }
mkdir -p mobile/releases/android mobile/releases/ios
gh release download "$tag" --pattern VoiceKeyboard.apk --dir mobile/releases/android --clobber
gh release download "$tag" --pattern VoiceKeyboard.ipa --dir mobile/releases/ios --clobber \
  || echo "No iOS build in $tag yet (the GitHub Actions build may still be running)" >&2
echo "Fetched $tag into mobile/releases/"
