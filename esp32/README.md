# ESP32 firmware

Makes an ESP32 or ESP32-C3 board a Voice Keyboard client: it connects to the
backend's `/v1/keyboard` socket over Wi-Fi (the same protocol as the desktop
client) and types what it receives as a Bluetooth LE keyboard (HID over GATT,
NimBLE). Users set it up from the web app's `/esp32` page, which flashes it
over Web Serial with esptool-js and then configures it over the same cable.

| File | |
|---|---|
| `main/main.c` | start-up, status, and the setup commands from the page |
| `main/io.c` | serial link to the page: UART0 and, on the C3, the built-in USB |
| `main/config.c` | settings in NVS (namespace `vk`) |
| `main/net.c` | Wi-Fi and the backend socket, pairing with the install token |
| `main/ble.c` | the Bluetooth keyboard and the typing queue |
| `main/keymap.c` | characters to key codes, US layout |

## Build

```bash
esp32/build.sh            # dev build of both chips, in Docker (espressif/idf:v5.4.2)
esp32/build.sh 1.3.0 esp32c3
```

Each chip gets one merged image, flashed at offset 0:
`esp32/dist/vkeyboard-esp32.bin` and `esp32/dist/vkeyboard-esp32c3.bin`.
`tools/release.sh` builds them and attaches them to the GitHub release; the
gateway mirrors them into `RELEASES_DIR/firmware/` and serves them at
`/client/vkeyboard-<chip>.bin`. To test a build without a release, copy it to
`backend/volumes/gateway/releases/firmware/`.

## Setup protocol

115200 baud. The page sends one JSON object per line; the board answers with
lines starting with `@vk ` (everything else is log output):

```
{"cmd":"info"}                     → @vk {"ev":"info","chip":"esp32c3","mac":…,"version":…,"configured":true,…}
{"cmd":"status"}                   → @vk {"ev":"status","wifi":"connected","server":"connected","ble":"advertising",…}
{"cmd":"scan"}                     → @vk {"ev":"scan","networks":[{"ssid":…,"rssi":-52,"secure":true}]}
{"cmd":"config","ssid":…,"pass":…,"server":…,"token":…,"name":…,"ca":…}   fields left out are kept
{"cmd":"type","text":"…"}          types over Bluetooth directly, for a test
{"cmd":"forget_bt"} {"cmd":"erase"} {"cmd":"reboot"}
```

A `status` event is also sent on every change. The Wi-Fi password and the
token are never sent back. `token` is the account's install token. After the
first connection the board stores the device credential the server issues,
like the desktop client does. `ca` is the PEM of Caddy's local CA
(`/ca.crt`), sent when the server uses it; otherwise the board trusts the
public CAs in ESP-IDF's bundle.
