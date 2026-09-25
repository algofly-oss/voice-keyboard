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

- creates `.env` (compose settings) and `backend/.env` (app settings) with a
  random admin password and session secret,
- picks `gpu` or `cpu`, fills in this machine's names and IP address for the
  HTTPS certificate, and chooses a free port (443, else 8271, else 8443),
- starts everything with `docker compose up -d --build`, waits until it is
  ready, and prints the address, the admin password, and how to trust the
  certificate on your devices.

Run it again at any time to redeploy; existing settings are kept. You can
also edit the two files and run `docker compose up -d --build` yourself.
The first start downloads the Whisper model, which takes a few minutes.

To have speech in any language typed as English, set `WHISPER_VARIANT=translate`
in `backend/.env` (uses Whisper large-v3). To switch between GPU and CPU, set
`COMPOSE_PROFILES=gpu` or `cpu` in `.env` and run `docker compose down` first.

## 2. HTTPS and the microphone

Browsers only let a page use the microphone in a *secure context*: HTTPS with a
certificate the browser trusts (or `localhost`). A plain `http://` address, or
an HTTPS certificate the browser does not trust ("Not secure"), means no
microphone. This repository takes care of that:

- **Everything is served over HTTPS on a single port** (`HTTPS_PORT` in
  `.env`). A bundled Caddy proxy terminates TLS; the app itself is not
  published. Plain `http://` requests to that port are redirected to HTTPS.
- **Certificates are automatic.**
  - *Local network* (default, `VK_TLS=internal`): Caddy runs its own small
    certificate authority and issues certificates for every name in
    `VK_DOMAIN` (IP addresses, host names, `.local` names). Each device must
    trust that authority once; after that the browser shows a normal
    padlock and allows the microphone.
  - *Public domain*: set `VK_DOMAIN=voice.example.com` and
    `VK_TLS=you@example.com`, and Caddy gets a Let's Encrypt certificate and
    renews it. This needs the domain to point at the server and port 443
    reachable from the internet (`HTTPS_PORT=443`). No per-device step.
- **Trusting the local authority on a device** (replace the address with your
  server's; `setup.sh` prints these with the right address):

  | Device | How |
  | --- | --- |
  | macOS / Linux | `curl -fsSLk https://<server>/client/trust-ca.sh \| sh -s -- https://<server>` |
  | Windows | `powershell -c "[Net.ServicePointManager]::ServerCertificateValidationCallback={$true}; irm https://<server>/client/trust-ca.ps1 \| iex"` |
  | iPhone / iPad | Open `https://<server>/ca.crt` in Safari (accept the warning once), install the profile, then turn it on in Settings → General → About → Certificate Trust Settings |
  | Android | Download `https://<server>/ca.crt`, then Settings → Security → Install a certificate → CA certificate |

  Restart the browser afterwards. The desktop client's install command
  (Settings → Clients) performs this step by itself. The `-k` flag only
  applies to downloading the certificate itself: nothing can be verified
  before it is trusted, just like accepting an SSH host key the first time.
  Everything after that is verified.
- **Keep `backend/volumes/caddy`.** It holds the certificate authority your
  devices trust; losing it means trusting a new one on every device.
- **Behind a tunnel or another proxy** (e.g. Cloudflare Tunnel): point it at
  `https://<server>:<HTTPS_PORT>` with certificate verification off (the tunnel
  does not know the local authority), and add the public name to
  `VK_DOMAIN`.

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
