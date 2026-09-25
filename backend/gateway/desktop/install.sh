#!/bin/sh
# Installs the Voice Keyboard desktop client on macOS or Linux.
#   curl -fsSL https://host/client/install.sh | sh -s -- --server https://host --token TOKEN
# Downloads one self-contained binary to ~/.local/bin, pairs it, and starts it
# in the background (and at every login). Nothing else is installed.
set -eu

SERVER=""
TOKEN=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --server) SERVER="$2"; shift 2 ;;
    --token) TOKEN="$2"; shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done
[ -n "$SERVER" ] && [ -n "$TOKEN" ] || { echo "Usage: install.sh --server URL --token TOKEN" >&2; exit 2; }

case "$(uname -s)" in
  Linux) os=linux ;;
  Darwin) os=darwin ;;
  *) echo "Unsupported system: $(uname -s)" >&2; exit 1 ;;
esac
case "$(uname -m)" in
  x86_64 | amd64) arch=amd64 ;;
  arm64 | aarch64) arch=arm64 ;;
  *) echo "Unsupported CPU: $(uname -m)" >&2; exit 1 ;;
esac

bin_dir="$HOME/.local/bin"
bin="$bin_dir/voice-keyboard"
mkdir -p "$bin_dir"
echo "Downloading voice-keyboard for $os/$arch..."
curl -fsSL "$SERVER/client/voice-keyboard-$os-$arch" -o "$bin.download"
chmod +x "$bin.download"

# Remove any previous installation before installing this one.
if [ -f "$bin" ] && [ "$(head -c 2 "$bin")" != "#!" ]; then
  "$bin" stop >/dev/null 2>&1 || true            # earlier release of this client
fi
legacy="${XDG_DATA_HOME:-$HOME/.local/share}/voice-keyboard"
# Only a Python interpreter running the old client (not e.g. an editor viewing it).
legacy_process='^[^ ]*python[0-9.]* [^ ]*voice_keyboard_client\.py'
if [ -d "$legacy/venv" ] || pgrep -f "$legacy_process" >/dev/null 2>&1; then
  echo "Removing the previous Python-based client..."
  pkill -f "$legacy_process" 2>/dev/null || true
  rm -rf "$legacy"
  # On macOS it kept its settings in ~/.config; this client uses Application Support.
  if [ "$os" = darwin ]; then rm -rf "$HOME/.config/voice-keyboard"; fi
fi
mv "$bin.download" "$bin"   # replaces the old launcher script, if any

"$bin" enroll --server "$SERVER" --token "$TOKEN"
"$bin" start

if [ "$os" = linux ] && [ -n "${WAYLAND_DISPLAY:-}" ] && [ ! -w /dev/uinput ]; then
  echo
  echo "Wayland: to type into all apps (not only X11 ones), allow the virtual keyboard once:"
  echo "  echo 'KERNEL==\"uinput\", TAG+=\"uaccess\"' | sudo tee /etc/udev/rules.d/60-voice-keyboard.rules"
  echo "  echo uinput | sudo tee /etc/modules-load.d/voice-keyboard.conf"
  echo "  sudo modprobe uinput && sudo udevadm control --reload && sudo udevadm trigger --name-match=uinput"
  echo "then run: voice-keyboard stop && voice-keyboard start"
fi
case ":$PATH:" in
  *":$bin_dir:"*) ;;
  *) echo "Add $bin_dir to your PATH to use the voice-keyboard command, or run $bin directly." ;;
esac
