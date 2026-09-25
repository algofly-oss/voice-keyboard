#!/bin/sh
# Publishes a mobile release: builds the signed Android APK, tags the current
# commit, and creates the GitHub release with the APK attached. The tag starts
# the iOS workflow, which adds VoiceKeyboard.ipa to the same release.
# Usage: tools/release_mobile.sh 1.0.0
set -eu
version=${1:?usage: $0 <version, e.g. 1.0.0>}
tag="voice-keyboard-v$version"
cd "$(dirname "$0")/.."

[ -z "$(git status --porcelain -- mobile)" ] || { echo "Commit mobile/ changes first" >&2; exit 1; }
git fetch -q origin
[ "$(git rev-parse HEAD)" = "$(git rev-parse '@{u}')" ] || { echo "Push your branch first" >&2; exit 1; }

(cd mobile/android && JAVA_HOME=${JAVA_HOME:-$HOME/Android/jdk21} ./gradlew -q assembleRelease)
apk=mobile/releases/android/VoiceKeyboard.apk
mkdir -p "$(dirname "$apk")"
cp mobile/android/app/build/outputs/apk/release/app-release.apk "$apk"

git tag -a "$tag" -m "Voice Keyboard $version"
git push -q origin "$tag"
gh release create "$tag" "$apk" --verify-tag --title "Voice Keyboard $version" \
  --notes "Android: VoiceKeyboard.apk. iOS: VoiceKeyboard.ipa is attached by GitHub Actions when its build finishes (unsigned; install with SideStore/AltStore)."
echo "Released $tag. Watch the iOS build with: gh run watch \$(gh run list --workflow voice-keyboard-ios.yml --limit 1 --json databaseId --jq '.[0].databaseId')"
