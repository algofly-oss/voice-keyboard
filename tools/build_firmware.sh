#!/usr/bin/env sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
CONFIG=${VK_ENV_FILE:-"$ROOT/esp32/.env"}
PORT=${1:-}

[ -f "$CONFIG" ] || { echo "Missing $CONFIG; copy esp32/.env.example to esp32/.env" >&2; exit 1; }
[ "$(command -v arduino-cli || true)" ] || { echo "arduino-cli is required" >&2; exit 1; }

set -a
. "$CONFIG"
set +a
python3 "$ROOT/tools/generate_firmware_config.py"
python3 "$ROOT/tools/build_web_app.py"

arduino-cli core install esp32:esp32
arduino-cli lib install NimBLE-Arduino "ESP Async WebServer" "Async TCP"
arduino-cli compile --fqbn esp32:esp32:esp32 --build-property build.partitions=huge_app "$ROOT/esp32"

if [ -n "$PORT" ]; then
  arduino-cli upload --fqbn esp32:esp32:esp32 --port "$PORT" "$ROOT/esp32"
fi

echo "Firmware built. Pass a serial port to upload, for example:"
echo "  $0 /dev/ttyUSB0"
