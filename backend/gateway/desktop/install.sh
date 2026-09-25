#!/usr/bin/env sh
set -eu

SERVER=""
TOKEN=""
ROOM="voice-keyboard"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --server) SERVER="$2"; shift 2 ;;
    --token) TOKEN="$2"; shift 2 ;;
    --room) ROOM="$2"; shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 2 ;;
  esac
done
[ -n "$SERVER" ] && [ -n "$TOKEN" ] || { echo "Server and enrollment token are required." >&2; exit 2; }

ROOT="${XDG_DATA_HOME:-$HOME/.local/share}/voice-keyboard"
mkdir -p "$ROOT"
python3 -m venv "$ROOT/venv"
"$ROOT/venv/bin/pip" install --upgrade pip >/dev/null
"$ROOT/venv/bin/pip" install -r "$SERVER/client/requirements.txt" >/dev/null
curl -fsSL "$SERVER/client/voice_keyboard_client.py" -o "$ROOT/voice_keyboard_client.py"
"$ROOT/venv/bin/python" "$ROOT/voice_keyboard_client.py" enroll --server "$SERVER" --token "$TOKEN" --room "$ROOM"
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/voice-keyboard" <<EOF
#!/usr/bin/env sh
exec "$ROOT/venv/bin/python" "$ROOT/voice_keyboard_client.py" "\$@"
EOF
chmod +x "$HOME/.local/bin/voice-keyboard"
echo "Installed. Start it with: voice-keyboard start"
