# Voice Keyboard

Self-hosted speech-to-text keyboard. Dictate in the browser, and the text is
typed into the focused window on your computer. Transcription runs on your own
GPU with Whisper.

## 1. Run the server

Requirements: Docker with Compose v2. An NVIDIA GPU (with the NVIDIA Container
Toolkit) is used if present; otherwise Whisper runs on the CPU, more slowly.

```bash
./setup.sh
```

On the first run `setup.sh`:

- creates `.env`, the one settings file (documented in `.env.example`), with a
  random admin password and session secret,
- picks `gpu` or `cpu`, fills in this machine's names and IP address for the
  HTTPS certificate, and chooses free ports (HTTPS 443, else 8272; HTTP 80, else 8271),
- starts everything with `docker compose up -d --build`, waits until it is
  ready, and prints the address, the admin password, and how to trust the
  certificate on your devices.

Run it again at any time to redeploy; existing settings are kept. You can
also edit `.env` and run `docker compose up -d --build` yourself.
The first start downloads the Whisper model, which takes a few minutes.

To have speech in any language typed as English, set `WHISPER_VARIANT=translate`
in `.env` (uses Whisper large-v3). In the normal mode, pick your language in
Settings (100 languages; English by default). **Hinglish** types Hindi in English
letters ("kya aap meri madad kar sakte hain"), and with English selected any
word Whisper writes in an Indian script is typed in English letters too. To switch between GPU and CPU, set
`COMPOSE_PROFILES=gpu` or `cpu` in `.env` and run `docker compose down` first.

## 2. HTTPS and the microphone

Browsers only allow the microphone over HTTPS with a certificate they trust.
Voice Keyboard handles this for you:

- A bundled Caddy proxy serves the web UI over **HTTPS** (`HTTPS_PORT`) and
  redirects plain HTTP (`HTTP_PORT`) to it. `VK_PROTOCOL=both` also serves the app
  over plain HTTP, for example for a Cloudflare tunnel; `VK_PROTOCOL=http` serves
  it only over plain HTTP, behind a proxy that adds HTTPS.
- **Certificates are automatic.** On a local network, Caddy signs them with its
  own certificate authority, and you trust that authority once per device
  (one command; `setup.sh` prints it). For a public domain with port 443 open,
  it uses Let's Encrypt and no device setup is needed.
- Every name or IP address people use to reach the server must be listed in
  `VK_DOMAIN` in `.env`.

**[docs/https.md](docs/https.md)** is the full guide:
- local network only;
- a public domain (Let's Encrypt);
- Cloudflare Tunnel;
- port forwarding (router, VPS, DuckDNS);
- behind an existing reverse proxy (Traefik, nginx);
- changing names and ports;
- troubleshooting (`ERR_SSL_PROTOCOL_ERROR`, "Not secure", `ERR_TOO_MANY_REDIRECTS`).

## 3. Open the web UI

Go to the address `setup.sh` printed (for example `https://192.168.1.20/`) and
sign in as `admin`.

<img src="docs/screenshots/web-ui.png" alt="Voice Keyboard web UI" width="280">

If the server becomes unreachable during dictation, the page keeps recording
and reconnects on its own.

## 4. Install the desktop client

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

`setup.sh` generates a long random `SESSION_SECRET` and admin password. Back
up `backend/volumes`: it holds the accounts database and the local CA that
your devices trust. If an install
command leaks, click **New command** in Settings → Clients. Remove a lost
client from the same list to revoke it.
