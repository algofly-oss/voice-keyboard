# Voice Keyboard

Self-hosted speech-to-text keyboard. Speak, and the text is typed into the app
you are using. Transcription runs on your own GPU with Whisper.

There are two ways to type:

- **Desktop client**: a small program on macOS, Windows or Linux types into the
  focused window.
- **ESP32**: a Bluetooth keyboard that types into any device, with nothing to
  install on it.

## 1. Run the backend

Requirements: Docker Compose and an NVIDIA GPU with the NVIDIA Container Toolkit.

```bash
cp backend/.env.example backend/.env   # set WEB_PASSWORD, API_KEY, SESSION_SECRET
docker compose up -d --build
curl http://localhost:8271/health      # {"status":"ok","whisper":true} once the model is loaded
```

The first start downloads the Whisper model, which takes a few minutes.

## 2. Open the web UI

Go to `http://localhost:8271/`, log in with `WEB_PASSWORD`, and tap the
microphone. To use it from another device, put the backend behind HTTPS, for
example with a reverse proxy or Cloudflare Tunnel; browsers only allow the
microphone on HTTPS or `localhost`.

<img src="docs/screenshots/web-ui.png" alt="Voice Keyboard web UI" width="280">

## 3. Install the desktop client

In the web UI open **Settings → Computers**, pick the operating system, and run
the command it shows on that computer:

```bash
# macOS / Linux
curl -fsSL https://your-host/client/install.sh | sh -s -- --server https://your-host --token …
```
```powershell
# Windows (PowerShell)
& ([scriptblock]::Create((irm 'https://your-host/client/install.ps1'))) -Server 'https://your-host' -Token '…'
```

It installs one self-contained program (x86-64 or ARM64) that runs in the
background and starts again after every restart. Dictate in the web UI, and the
text is typed on that computer.

```bash
voice-keyboard status   # also: stop, start, uninstall
```

On macOS, allow it under **Privacy & Security → Accessibility** when prompted.
Details: [desktop/README.md](desktop/README.md).

## ESP32 hardware keyboard

The ESP32 acts as a Bluetooth keyboard, so it types into computers, tablets,
TVs, or anything else that accepts one. It serves its own copy of the web UI:
open its URL, dictate, and the ESP32 types the text.

A second mode is planned: an I2S digital microphone and physical buttons on the
board, so you can dictate without a browser. The design is in
[esp32/HARDWARE.md](esp32/HARDWARE.md). Build and flashing instructions are in
[esp32/README.md](esp32/README.md).

## Security

Set `WEB_PASSWORD`, `API_KEY` and `SESSION_SECRET` before exposing the backend,
and use HTTPS outside your LAN. If an install command leaks, click **New
command** in Settings → Computers to replace it; computers already installed
stay connected.
