#!/bin/sh
# Installs the Voice Keyboard desktop client (vkeyboard) on macOS or Linux.
#   curl -fsSL https://host/client/install.sh | sh -s -- --server https://host --token TOKEN
# Downloads one self-contained binary to ~/.local/bin, removes any older
# version, pairs it, and starts it in the background (and at every login).
set -eu

SERVER=""
TOKEN=""
TRUST_CA=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --server) SERVER="$2"; shift 2 ;;
    --token) TOKEN="$2"; shift 2 ;;
    --trust-local-ca) TRUST_CA=1; shift ;;   # the server signs with its own local CA
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

# A server with its own local CA: trust it first (this client and browsers need
# it). Only this first download is unverified; everything after is checked.
if [ -n "$TRUST_CA" ] && ! curl -fsS "$SERVER/health" >/dev/null 2>&1; then
  echo "Trusting the server's certificate authority..."
  curl -fsSLk "$SERVER/client/trust-ca.sh" | sh -s -- "$SERVER"
  curl -fsS "$SERVER/health" >/dev/null || { echo "Still cannot reach $SERVER over verified HTTPS" >&2; exit 1; }
fi

bin_dir="$HOME/.local/bin"
bin="$bin_dir/vkeyboard"
mkdir -p "$bin_dir"
echo "Downloading vkeyboard for $os/$arch..."
curl -fsSL "$SERVER/client/vkeyboard-$os-$arch" -o "$bin.download"
chmod +x "$bin.download"

# --- Remove older versions before installing this one.
is_binary() { [ -f "$1" ] && [ "$(head -c 2 "$1")" != "#!" ]; }
# 1. An earlier vkeyboard (upgrade in place).
if is_binary "$bin"; then "$bin" stop >/dev/null 2>&1 || true; fi
# 2. Releases before 1.3, named voice-keyboard. `vkeyboard start` below also
#    removes their login item and settings, keeping the machine id.
old="$bin_dir/voice-keyboard"
if [ -e "$old" ]; then
  echo "Removing the previous voice-keyboard client..."
  if is_binary "$old"; then "$old" stop >/dev/null 2>&1 || true; fi
  rm -f "$old"
fi
# 3. The original Python client. Match only a Python interpreter running it,
#    not e.g. an editor that has the file open.
legacy="${XDG_DATA_HOME:-$HOME/.local/share}/voice-keyboard"
legacy_process='^[^ ]*python[0-9.]* [^ ]*voice_keyboard_client\.py'
if [ -d "$legacy" ] || pgrep -f "$legacy_process" >/dev/null 2>&1; then
  echo "Removing the previous Python-based client..."
  pkill -f "$legacy_process" 2>/dev/null || true
  rm -rf "$legacy"
  # On macOS it kept its settings in ~/.config.
  if [ "$os" = darwin ]; then rm -rf "$HOME/.config/voice-keyboard"; fi
fi
mv "$bin.download" "$bin"

"$bin" enroll --server "$SERVER" --token "$TOKEN"
"$bin" start

if [ "$os" = linux ] && [ -n "${WAYLAND_DISPLAY:-}" ] && [ ! -w /dev/uinput ]; then
  echo
  echo "Wayland: to type into all apps (not only X11 ones), allow the virtual keyboard once:"
  echo "  echo 'KERNEL==\"uinput\", TAG+=\"uaccess\"' | sudo tee /etc/udev/rules.d/60-vkeyboard.rules"
  echo "  echo uinput | sudo tee /etc/modules-load.d/vkeyboard.conf"
  echo "  sudo modprobe uinput && sudo udevadm control --reload && sudo udevadm trigger --name-match=uinput"
  echo "then run: vkeyboard stop && vkeyboard start"
fi
case ":$PATH:" in
  *":$bin_dir:"*) ;;
  *) echo "Add $bin_dir to your PATH to use the vkeyboard command, or run $bin directly." ;;
esac
