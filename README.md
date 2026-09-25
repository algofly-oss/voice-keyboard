# Voice Keyboard

Self-hosted speech-to-text keyboard. Dictate in the browser, and the text is
typed into the focused window on your computer. Transcription runs on your own
GPU with Whisper.

## 1. Run the backend

Requirements: Docker Compose. With an NVIDIA GPU (the default) you also need
the NVIDIA Container Toolkit. To run on the CPU instead, set
`COMPOSE_PROFILES=cpu` in `./.env`; this works with or without a GPU and is
slower. Run `docker compose down` before switching.

To have speech in any language typed as English, set `WHISPER_VARIANT=translate`
in `backend/.env` (uses Whisper large-v3).

```bash
cp .env.example .env                   # compose settings: GPU/CPU, HTTPS names and ports
cp backend/.env.example backend/.env   # app settings: set WEB_PASSWORD and SESSION_SECRET
docker compose up -d --build
curl http://localhost:8271/health      # {"status":"ok","whisper":true} once the model is loaded
```

The first start downloads the Whisper model, which takes a few minutes.

## 2. Open the web UI

Go to `https://localhost/` and sign in as `admin` with `WEB_PASSWORD`.

HTTPS is on by default, because browsers only allow the microphone on HTTPS.
The bundled Caddy proxy gets certificates automatically:

- **On a LAN** (default, `VK_TLS=internal`): list the server's names or IP
  addresses in `VK_DOMAIN` in `.env`. Caddy signs them with its own local CA.
  Trust that CA once on each device, and the browser stops warning and allows
  the microphone:
  - macOS / Linux: `curl -fsSL http://<server>/client/trust-ca.sh | sh -s -- http://<server>`
  - Windows (admin PowerShell): `curl.exe -o vk-ca.crt http://<server>/ca.crt; certutil -addstore -f Root vk-ca.crt`
  - iPhone/iPad: open `http://<server>/ca.crt`, install the profile, then turn it
    on in Settings → General → About → Certificate Trust Settings.
  - Android: download `http://<server>/ca.crt`, then Settings → Security →
    Install a certificate → CA certificate.
- **Public domain**: set `VK_DOMAIN=voice.example.com` and `VK_TLS=you@example.com`.
  Caddy then gets a Let's Encrypt certificate, which needs ports 80 and 443
  reachable from the internet.

Ports `HTTPS_PORT`/`HTTP_PORT` default to 443/80. The gateway's plain-HTTP port
(8271) is published on `127.0.0.1` only unless `API_BIND` says otherwise.

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

Use a long random `SESSION_SECRET`. Back up `backend/volumes/caddy`: it holds
the local CA that your devices trust. If an install
command leaks, click **New command** in Settings → Clients. Remove a lost
client from the same list to revoke it.
