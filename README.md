# ESP32 Voice Keyboard

Dictate on your phone, and text appears on a paired computer or iPad. The ESP32 is
a **Bluetooth LE keyboard** and serves a web app. The app streams your voice to a
self-hosted Whisper backend while you speak. When you stop, the full transcript
is ready almost immediately, and the ESP32 types it.

```
Phone browser ──HTTPS proxy──▶ ESP32  (web app, settings, typing)
      │                          └──BLE keyboard──▶ iPad / computer
      └──WebSocket, live audio──▶ Backend :8000  (gateway + Whisper, GPU)
```

Audio never passes through the ESP32. The browser streams 16 kHz PCM to the
backend and sends only the final text to the ESP32.

## 1. Backend

One container and one published port. The image extends
`fedirz/faster-whisper-server` with a small FastAPI gateway
(`backend/gateway/app.py`) that runs next to Whisper. Whisper only listens on
the container's localhost.

```bash
cd backend
cp .env.example .env   # optional: port, model, API key
docker compose up -d --build
curl localhost:8000/health   # {"status":"ok","whisper":true}
```

| Endpoint | Purpose |
|---|---|
| `WS /v1/stream` | Live dictation (protocol in `app.py`) |
| `WS /v1/events?room=<hostname>` | Keeps every open web UI in sync; also stops or discards another device's dictation |
| `GET /health` | 200 when Whisper is ready, 503 while it starts |
| `/v1/*` | OpenAI-compatible Whisper API passed through, e.g. `POST /v1/audio/transcriptions` |

While you speak, the gateway cuts the stream into phrases using Silero VAD,
which ships with the Whisper image.

| | Live (Type while speaking) | Batch |
|---|---|---|
| Cut at a pause of | 0.4 s, once ≥ 1 s is collected | 0.6 s, once ≥ 6 s is collected |
| Non-stop speech | next gap between words after 5 s, forced at 8 s | after 20 s, forced at 28 s |

When a cut falls mid-sentence, the gateway holds back Whisper's full stop and
decides at the next phrase whether the sentence continues. Only one dictation
per keyboard runs at a time. Each chunk is transcribed while recording continues, so a
60-second dictation finishes about 0.35 s after you stop. The model is loaded at start-up, so the
first dictation is fast too.

`API_KEY` in `backend/.env` (git-ignored) protects everything except `/health`.
Clients send it as a `Bearer` token or as `?token=`. The same key is the
firmware default (`DEFAULT_API_KEY`) and can be changed in Settings → Server.
Translation is not offered: large-v3-turbo was trained without translation
data and returns the original language.

## 2. ESP32 firmware

1. Arduino IDE with ESP32 core 3.x. Install from Library Manager:
   **NimBLE-Arduino** (h2zero, 2.x), **ESP Async WebServer** and **Async TCP**
   (both ESP32Async, 3.x).
2. Select **ESP32 Dev Module** and partition scheme **Huge APP (3MB No OTA/1MB
   SPIFFS)**, then upload `voice_keyboard.ino`.
3. The serial monitor (115200 baud) prints `http://<ip>/` and
   `http://voice-keyboard.local/`.

The web app source is `web/index.html`. After editing it, run
`python3 tools/build_web_app.py` to regenerate `web_app.h` (a gzipped copy
with an ETag).

Notes:

- **Bluetooth stack:** NimBLE leaves about 100 KB more heap than the core's
  Bluedroid stack.
- **Web server:** ESPAsyncWebServer serves many connections at once. The
  core's `esp_http_server` rejects requests with more than 1024 bytes of
  headers in total, which happens behind Cloudflare once cookies are added.
  The synchronous `WebServer` stalls for seconds on each idle connection that a
  browser or proxy opens.
- **Wi-Fi speed:** Wi-Fi and Bluetooth share one radio, so throughput drops
  while a device is connected. Keep the ESP32 within good range of the router.
  The page shows a weak signal as slow loads.
- **Brownouts:** if the serial log shows `Brownout detector was triggered`,
  use a better USB cable or port, a powered hub, or a 470–1000 µF capacitor
  across 5V/GND.

## 3. HTTPS proxy

Browsers only allow the microphone on HTTPS pages. Put an HTTPS reverse proxy
in front of both services:

- **Web app:** `https://keyboard.example` → `http://<esp32-ip>/`
- **Backend:** `https://whisper.example` → `http://<backend-host>:8000`
  - WebSocket upgrades must be enabled.
  - Set idle timeouts of a few minutes for long dictations.

Then set **Settings → Server → Server URL** to the backend's HTTPS URL. A path
on the same host, such as `/whisper`, also works if the proxy routes it.

## 4. Use it

The web app asks for a password first. The default is `voicekeyboard`
(`DEFAULT_WEB_PASSWORD` in the firmware). Change it under Settings → Device;
any text or number works. A login lasts a year on that browser. Changing the
password signs out all other browsers.

1. Pair the iPad or computer with **ESP32 Voice Keyboard**.
2. Open the web app on your phone (you can add it to the Home Screen).
3. Tap the microphone and speak. Tap it again to stop, or tap **Discard**.
   The transcript is typed on the paired device.
4. **Backspace** deletes one character per tap and repeats while held.
   **Enter** presses Return.
5. **Disconnect** drops the Bluetooth link so the iPad shows its on-screen
   keyboard again. **Connect**, or starting a dictation, reconnects it.
6. Several phones can have the app open at once. While one dictates, the others
   mirror it (timer, waveform, live caption), and can stop or discard it.

## Settings

| Tab | Setting | Notes |
|---|---|---|
| General | Type while speaking | On: each phrase is typed as soon as it is recognized (about 3–5 s behind you). Off: everything is typed when you stop |
| | Language | Auto-detect, or pick one for speed and accuracy |
| | After dictation | Nothing (default) / Space / Enter, typed once after the transcript |
| | Stop recording after | 1 minute to 1 hour |
| | Typing speed | Ultra fast (default) rolls from key to key (one report per character); slow it down if characters go missing |
| Server | Server URL, API key | The backend gateway; **Test connection** checks it |
| | Advanced: Model, Vocabulary | Vocabulary helps with names and jargon |
| Device | Bluetooth name, Wi-Fi, hostname | Saving restarts the ESP32 |
| | Web app password, Sign out | Also shows IP address, `.local` name, Wi-Fi signal |

If Wi-Fi fails, the ESP32 opens a setup network, **VoiceKeyboard-Setup**
(password `voicekeyboard`), at http://192.168.4.1/.

## ESP32 HTTP API

All `/api/*` endpoints need the session cookie from `POST /api/login`
(`password=...`), except login and logout themselves.

| Endpoint | Purpose |
|---|---|
| `POST /api/login`, `POST /api/logout` | Start or end a session (cookie `vk_session`) |
| `GET /api/status` | Bluetooth, typing state, IP, RSSI, heap |
| `GET/POST /api/settings` | Read settings, or update any subset (form-encoded) |
| `POST /api/type?after=1` | Type the body text; `after=1` appends the "after dictation" key |
| `POST /api/key?k=backspace&s=down\|hold\|up` | Held backspace; stops repeating if `hold` is not renewed within 500 ms |
| `POST /api/key?k=enter` | Press Enter |
| `POST /api/bluetooth?on=0\|1` | Disconnect and stop advertising / advertise again |
| `POST /api/cancel` | Stop typing and clear the queue |

## Limitations

- Typing uses the US keyboard layout. Accented letters and typographic quotes
  are converted to ASCII.
