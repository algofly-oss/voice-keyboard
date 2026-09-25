# Voice Keyboard

Self-hosted speech-to-text keyboard. Dictate in the browser, and the text is
typed into the focused window on your computer. Transcription runs on your own
GPU with Whisper.

## 1. Run the backend

Requirements: Docker Compose and an NVIDIA GPU with the NVIDIA Container Toolkit.

```bash
cp backend/.env.example backend/.env   # set WEB_PASSWORD and SESSION_SECRET
docker compose up -d --build
curl http://localhost:8271/health      # {"status":"ok","whisper":true} once the model is loaded
```

The first start downloads the Whisper model, which takes a few minutes.

## 2. Open the web UI

Go to `http://localhost:8271/` and sign in as `admin` with `WEB_PASSWORD`. To
use it from another device, put the backend behind HTTPS, for example with a
reverse proxy or Cloudflare Tunnel; browsers only allow the microphone on
HTTPS or `localhost`.

<img src="docs/screenshots/web-ui.png" alt="Voice Keyboard web UI" width="280">

If the server becomes unreachable during dictation, the page keeps recording
and reconnects on its own.

## 3. Install the desktop client

In the web UI open **Settings → Clients**, pick the operating system, and run
the command it shows on the computer you want to type on:

```bash
# macOS / Linux
curl -fsSL https://your-host/client/install.sh | sh -s -- --server https://your-host --token …
```
```powershell
# Windows (PowerShell)
& ([scriptblock]::Create((irm 'https://your-host/client/install.ps1'))) -Server 'https://your-host' -Token '…'
```

It installs one self-contained program (x86-64 or ARM64) that runs in the
background, starts after every restart, and reconnects on its own. Every
installed client is listed under **Settings → Clients** with its status. Tap
the one that should type; only that active client receives your dictation.

```bash
vkeyboard status   # connected? selected to type?  Also: logs, stop, start, uninstall
```

On macOS, allow it under **Privacy & Security → Accessibility** when prompted.
Details are in [desktop/README.md](desktop/README.md).

## Accounts

The first account (`admin`) is created from `WEB_PASSWORD`. Its username and
password can be changed later under **Settings → Account**. Set `ALLOW_SIGNUPS=true` to
let others create accounts on the login page; it is off by default. Each account
has its own clients, install command, and dictation. All accounts share the
one Whisper model: if two people dictate at the same moment, their requests
wait their turn (`WHISPER_CONCURRENCY`, default 1).

## Security

Use HTTPS outside your LAN and a long random `SESSION_SECRET`. If an install
command leaks, click **New command** in Settings → Clients. Remove a lost
client from the same list to revoke it.
