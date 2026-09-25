# ESP32 hardware client

This directory contains the optional ESP32 implementation. The board acts as
both a Wi-Fi web server/client and a Bluetooth Low Energy HID keyboard. Audio
does not pass through the ESP32: a browser sends PCM audio to the Whisper
backend, and the ESP32 receives text and emits HID reports.

## Supported hardware

The firmware targets an ESP32 development board supported by the Arduino ESP32
core 3.x. Select **ESP32 Dev Module** unless your board vendor specifies a
different variant. Boards must provide Wi-Fi and BLE; ESP32-C3/S3 variants may
need small compatibility adjustments depending on their Arduino core version.

## Required software

- Arduino IDE 2.x: <https://www.arduino.cc/en/software>
- Espressif Arduino core for ESP32, 3.x: install through Boards Manager using
  `https://espressif.github.io/arduino-esp32/package_esp32_index.json`
- NimBLE-Arduino by h2zero, 2.x:
  <https://github.com/h2zero/NimBLE-Arduino>
- ESPAsyncWebServer by ESP32Async, 3.x:
  <https://github.com/ESP32Async/ESPAsyncWebServer>
- AsyncTCP by ESP32Async, 3.x:
  <https://github.com/ESP32Async/AsyncTCP>

Install the three libraries through Arduino IDE → Library Manager, or clone
the linked repositories into the Arduino libraries directory. The firmware's
remaining headers (`WiFi`, `Preferences`, `ESPmDNS`, `mbedtls`, and BLE HID
types) are supplied by the ESP32 board core and do not need separate installs.

## Reproducible Arduino IDE setup

1. Install Arduino IDE 2.x.
2. Add the Espressif Boards Manager URL above under Preferences → Additional
   Board Manager URLs.
3. Install **esp32 by Espressif Systems**, major version 3.
4. Install the three libraries listed above.
5. Open `voice_keyboard.ino`.
6. Select **ESP32 Dev Module**.
7. Select the correct serial port.
8. Set Partition Scheme to **Huge APP (3MB No OTA/1MB SPIFFS)**.
9. Set Upload Speed to 921600; use 115200 if uploads are unreliable.
10. Compile first, then click Upload. Hold the board's BOOT button when the
    IDE asks for download mode on boards that do not auto-reset.

## Embedded web UI build

`web/index.html` is the source UI. `web_app.h` is generated compressed data;
do not hand-edit it. From the repository root run:

```bash
python3 tools/build_web_app.py
```

Then reopen the sketch and upload again. The generator writes the output to
`esp32/web_app.h` and preserves a deterministic gzip payload and ETag.

## First boot and operation

The serial monitor uses 115200 baud. On first boot the firmware prints its IP
address and mDNS address, normally `http://voice-keyboard.local/`. If Wi-Fi is
not configured it starts the `VoiceKeyboard-Setup` access point with password
`voicekeyboard`; open `http://192.168.4.1/` to configure it.

Pair the advertised **ESP32 Voice Keyboard** device with the target computer,
iPhone, or iPad. Open the web UI on a phone, configure the backend server, and
tap the microphone. The ESP32 receives the recognized text and types it into
the active application.

## Command-line build alternative

For CI or repeatable builds, install Arduino CLI and the same Espressif board
package and libraries. The exact board package and library versions should be
pinned in CI before publishing release firmware; the Arduino IDE workflow above
is the reference setup until a formal `arduino-cli.yaml` is added.

## Troubleshooting

- **Upload fails:** select the right port, use a data-capable USB cable, lower
  upload speed, or hold BOOT during reset.
- **Brownout detector:** use a powered USB hub or better cable and add a
  470–1000 µF capacitor across 5V and GND if the supply is marginal.
- **No BLE connection:** keep the board close to the target, remove stale
  pairings, and power-cycle both devices.
- **Slow web UI:** Wi-Fi and Bluetooth share the radio; improve signal and
  reduce distance from the access point.
- **Memory problems:** retain NimBLE rather than switching to the heavier
  Bluedroid stack; the firmware is designed around the available ESP32 heap.
- **Proxy/header failures:** ESPAsyncWebServer is required; the synchronous
  web server can stall while browsers keep connections open.

## Firmware boundaries

The Arduino code owns BLE HID reports, Wi-Fi setup, device settings, the local
web server, and the ESP32 HTTP API. The backend owns Whisper, VAD, streaming
transcription, and desktop-client routing. Keep those responsibilities separate
when adding features so the ESP32 remains usable without the desktop daemon.
The public backend gateway normally listens on host port `8271`; its private Whisper
transcription process uses port `8001` inside the backend container. Configure
the ESP32 and browser with the gateway URL, never the private Whisper port.
