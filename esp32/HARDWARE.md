# Hardware expansion plan

This document defines the hardware boundary before the I2S microphone and
physical controls are wired. Do not connect parts solely from a GPIO number in
this document until the exact board revision is confirmed.

## Functional controls

| Control | Function | Runtime behavior |
|---|---|---|
| Mic button | Start/stop dictation | Starts browser or onboard-mic capture according to the saved source setting |
| Keyboard button | Temporary BLE HID toggle | Stops/starts advertising and disconnects/reconnects the paired keyboard; does not change the saved mic setting |
| Single red LED | Dictation indicator | Solid on while the selected microphone is actively recording/streaming; off otherwise |

The firmware should use one shared state machine for physical buttons and web
controls. A button press must not create a second transcription session when a
browser or another control already owns the session.

## Microphone design

The planned onboard microphone is an I2S digital microphone. The ESP32 should
capture 16 kHz, mono, signed PCM and stream it to the same backend protocol as
the browser. Whisper remains on the backend; the ESP32 only performs capture,
buffering, Wi-Fi transport, and status management.

The saved setting will be an enum such as `browser` or `onboard`. Exactly one
source may be active. The setting is stored in `Preferences`, restored on boot,
and exposed in both web settings and future physical-control status messages.

Use a board with sufficient RAM and, preferably, PSRAM for the onboard-mic
profile. Keep audio buffers bounded and feed the network task from a FreeRTOS
queue so BLE HID typing is not starved.

## Pin policy

The original ESP32 Dev Module and ESP32-C3 do not share the same GPIO map.
GPIO numbers must therefore be selected by a board profile, not scattered as
numeric literals through the firmware.

Before final wiring, record the exact board and module revision and assign:

- I2S BCLK/SCK
- I2S WS/LRCLK
- I2S data-in/SD
- microphone power or enable, if present
- microphone button input
- keyboard button input
- red LED output
- optional status LED output

Avoid flash/PSRAM pins, boot-strapping pins, USB pins, and pins unavailable on
the selected C3 package. Prefer internal pull-ups with buttons wired to ground
when the board supports them. Add debouncing in software and require a stable
state for roughly 30–50 ms.

## Test sequence when parts arrive

1. Identify the exact board and photograph its pin labels.
2. Run a GPIO continuity test with the ESP32 disconnected from the microphone.
3. Run an LED/button test and verify active-low versus active-high wiring.
4. Run an I2S microphone test that prints RMS level and confirms silence/speech.
5. Test browser microphone mode with the onboard microphone disabled.
6. Test onboard microphone mode with the browser source disabled.
7. Test live mode, stop behavior, reconnects, and BLE typing.
8. Test the keyboard button while idle, recording, transcribing, and typing.
9. Confirm settings survive reboot and browser reload.

The single LED intentionally has no blink or multi-state protocol: off means
the microphone is inactive, and solid red means it is active. BLE connection,
transcription progress, Wi-Fi, and errors remain visible in the web UI. This
avoids confusing LED patterns and makes the physical control safe to understand
at a glance.

The exact final wiring diagram and board profile should be committed here after
the ordered component part numbers are known.
