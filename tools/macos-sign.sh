#!/bin/sh
# Signs the macOS client builds with the project's own certificate, so that
# an updated client is the same app to macOS and keeps its Accessibility
# permission (an ad-hoc signature pins the permission to one build). The
# designated requirement is: this identifier, signed with this certificate
# (see tools/macos-requirement.py). Runs on Linux, with rcodesign.
#
#   tools/macos-sign.sh <binary>...          sign (tools/release.sh does this)
#   tools/macos-sign.sh --create-certificate once per release machine
#
# The certificate and key live in $VK_MACOS_SIGNING_DIR (default
# ~/.config/vkeyboard-release), outside the repository. Back them up: with a
# new certificate, every Mac has to allow Accessibility once more.
set -eu
dir=${VK_MACOS_SIGNING_DIR:-$HOME/.config/vkeyboard-release}
identifier=ai.algofly.vkeyboard
rcodesign_version=0.29.0
here=$(cd "$(dirname "$0")" && pwd)

rcodesign=$(command -v rcodesign || true)
if [ -z "$rcodesign" ]; then
  rcodesign=$HOME/.cache/vkeyboard-release/rcodesign-$rcodesign_version
  if [ ! -x "$rcodesign" ]; then
    arch=$(uname -m); [ "$arch" = arm64 ] && arch=aarch64
    name=apple-codesign-$rcodesign_version-$arch-unknown-linux-musl
    echo "Downloading rcodesign $rcodesign_version..."
    mkdir -p "$(dirname "$rcodesign")"
    curl -fsSL "https://github.com/indygreg/apple-platform-rs/releases/download/apple-codesign/$rcodesign_version/$name.tar.gz" |
      tar -xzO "$name/rcodesign" > "$rcodesign.part"
    chmod +x "$rcodesign.part" && mv "$rcodesign.part" "$rcodesign"
  fi
fi

if [ "${1:-}" = --create-certificate ]; then
  [ ! -e "$dir/macos-signing.key" ] || { echo "$dir/macos-signing.key exists; not replacing it" >&2; exit 1; }
  (umask 077 && mkdir -p "$dir")
  (umask 077 && "$rcodesign" generate-self-signed-certificate --person-name "Voice Keyboard" \
    --validity-days 7300 --pem-filename "$dir/macos-signing")
  echo "Created $dir/macos-signing.{crt,key}. Back them up."
  exit 0
fi

[ $# -gt 0 ] || { echo "usage: $0 <binary>... | --create-certificate" >&2; exit 2; }
if [ ! -r "$dir/macos-signing.key" ]; then
  echo "No macOS signing certificate in $dir." >&2
  echo "Restore the backup there, or (only for a new project) run: $0 --create-certificate" >&2
  exit 1
fi
requirement=$(mktemp)
trap 'rm -f "$requirement"' EXIT
python3 "$here/macos-requirement.py" "$dir/macos-signing.crt" "$identifier" "$requirement" >/dev/null
for binary in "$@"; do
  "$rcodesign" sign --pem-file "$dir/macos-signing.crt" --pem-file "$dir/macos-signing.key" \
    --binary-identifier "$identifier" --code-requirements-file "$requirement" "$binary" >/dev/null 2>&1 ||
    { echo "signing $binary failed" >&2; "$rcodesign" sign --pem-file "$dir/macos-signing.crt" --pem-file "$dir/macos-signing.key" \
        --binary-identifier "$identifier" --code-requirements-file "$requirement" "$binary"; exit 1; }
  "$rcodesign" print-signature-info "$binary" | grep -q "designated(3): 0: (identifier \"$identifier\")" ||
    { echo "$binary: the designated requirement is missing after signing" >&2; exit 1; }
  echo "signed $(basename "$binary")"
done
