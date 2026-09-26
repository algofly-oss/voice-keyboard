# HTTPS setup guide

Browsers only allow the microphone on a **secure context**: a page served over
HTTPS with a certificate the browser trusts (or `http://localhost` on the
machine itself). Voice Keyboard therefore always serves the web UI over HTTPS.
This guide explains how that works and how to set it up for your network or
domain.

## How it works

```
browser ──HTTPS──▶ Caddy (container "https") ──HTTP, inside Docker──▶ gateway ──▶ Whisper
                   the only published port: HTTPS_PORT
```

- **Caddy is the only way in.** It publishes `HTTPS_PORT` and `HTTP_PORT` (from
  `.env`). The gateway and Whisper are reachable only inside Docker's network.
- **`VK_PROTOCOL` decides what each port does:**

  | `VK_PROTOCOL` | `HTTPS_PORT` | `HTTP_PORT` | Use it for |
  | --- | --- | --- | --- |
  | `https` (default) | the app | redirects to HTTPS; serves `/ca.crt` | LAN, public domain, port forwards |
  | `both` | the app | the app, no redirect | HTTPS on the LAN **and** plain HTTP for a Cloudflare tunnel |
  | `http` | not used | the app | only behind a tunnel/proxy that adds HTTPS |

  Plain HTTP is only useful behind something that adds HTTPS (such as Cloudflare):
  browsers block the microphone on plain `http://` except at `localhost`.
  To keep a port off the network, set `HTTPS_BIND=127.0.0.1` or `HTTP_BIND=127.0.0.1`.
- **Plain HTTP to the HTTPS port is redirected** to `https://`.
- **Certificates are automatic.** Caddy issues and renews a certificate for
  every name listed in `VK_DOMAIN`, from one of two sources:

  | `VK_TLS` | Certificates from | Browser trust |
  | --- | --- | --- |
  | `internal` (default) | Caddy's own local certificate authority (CA), created on first start | Trust the CA **once per device** |
  | an email address | Let's Encrypt | Trusted everywhere automatically |

- **Names matter.** A browser sends the name it is connecting to, and Caddy
  only answers for names in `VK_DOMAIN`. Every name or IP address people type
  must be in that list, including names used through tunnels and port
  forwards. A missing name shows up as `ERR_SSL_PROTOCOL_ERROR`.

All settings are in the root `.env`. `./setup.sh` creates it on the first run,
and `.env.example` documents it:

```bash
COMPOSE_PROFILES=gpu           # or cpu
VK_DOMAIN=192.168.1.20, myserver, localhost   # every name/IP clients use, comma-separated
VK_DEFAULT_SNI=192.168.1.20    # certificate for clients that connect by IP (they send no name)
VK_TLS=internal                # or your email address for Let's Encrypt
VK_PROTOCOL=https              # https, both or http (see the table above)
HTTPS_PORT=443
HTTP_PORT=80
```

After changing any of these, apply them with:

```bash
docker compose up -d
```

## Scenario 1: local network only (default)

Use this when people reach the server by IP address, host name or `.local` name
inside your network.

1. Run `./setup.sh`. It fills in the machine's IP address and host name and
   chooses free ports (HTTPS 443, then 8272; HTTP 80, then 8271).
2. Add any other names you use to `VK_DOMAIN`, such as `myserver.local` or a
   name from your router's DNS. Then run `docker compose up -d`.
3. On **each device**, trust the local CA once, and restart the browser:

   | Device | Command / steps |
   | --- | --- |
   | macOS, Linux | `curl -fsSLk https://<server>/client/trust-ca.sh \| sh -s -- https://<server>` |
   | Windows | `powershell -c "[Net.ServicePointManager]::ServerCertificateValidationCallback={$true}; irm https://<server>/client/trust-ca.ps1 \| iex"` |
   | iPhone, iPad | Open `https://<server>/ca.crt` in Safari (accept the warning once). Install the downloaded profile in Settings, then turn it on under Settings → General → About → Certificate Trust Settings |
   | Android | Download `https://<server>/ca.crt`, then Settings → Security → Encryption & credentials → Install a certificate → CA certificate |

   `<server>` is the address you open, with its port if it isn't 443, for
   example `https://192.168.1.20:8271`. The desktop client's install command
   (Settings → Clients) does this step by itself.

Notes:

- The scripts fetch `ca.crt` with certificate checks off (`-k`), because
  nothing can be verified before the CA is trusted. It is the same as accepting
  an SSH host key the first time. Everything after that is verified.
- Linux: Chrome and Firefox keep their own certificate store. The script adds
  the CA there too when `certutil` is installed (`libnss3-tools` on
  Debian/Ubuntu, `nss-tools` on Fedora).
- Firefox on Windows and macOS: set `security.enterprise_roots.enabled` to
  `true` in `about:config` so it uses the system's trusted CAs.

## Scenario 2: public domain with ports 80/443 (Let's Encrypt)

Use this when the server has a public IP address and you own a domain.

1. Point the domain's DNS `A`/`AAAA` record at the server's public IP.
2. Make sure port **443** on that IP reaches the server, which means opening or
   forwarding it on the firewall or router. Let's Encrypt checks it from the
   internet.
3. In `.env`:

   ```bash
   VK_DOMAIN=voice.example.com
   VK_TLS=you@example.com   # used by Let's Encrypt for expiry notices
   HTTPS_PORT=443
   ```

4. Run `docker compose up -d`. Caddy obtains the certificate within seconds and
   renews it automatically. No device needs any setup.

Let's Encrypt only validates on the standard port 443, so this doesn't work if
another program already uses 443 on the server, or if you can only forward a
different port. Use scenario 3, 4 or 5 in those cases.

## Scenario 3: Cloudflare Tunnel

Use this when the server isn't reachable from the internet and a
`cloudflared` tunnel publishes it, for example as `voice.example.com`.
Cloudflare presents its own trusted certificate to visitors, so no device needs
any setup. The tunnel can reach the server in either of two ways.

**Option A: plain HTTP (simplest).** Set `VK_PROTOCOL=both`, which keeps HTTPS on
`HTTPS_PORT` for the LAN, or `VK_PROTOCOL=http`. Then point the tunnel's public
hostname at **HTTP** `<server-lan-ip>:<HTTP_PORT>`, for example
`http://192.168.1.20:8271`. Nothing else is needed.

**Option B: HTTPS to the server.** Keep `VK_PROTOCOL=https`:

1. Add the public name to `VK_DOMAIN` in `.env`, for example
   `VK_DOMAIN=192.168.1.20, voice.example.com`, and run `docker compose up -d`.
2. In the Cloudflare dashboard: **Zero Trust → Networks → Tunnels →** your
   tunnel **→ Public Hostname →** the hostname:
   - **Service:** type **HTTPS**, URL `<server-lan-ip>:<HTTPS_PORT>`, for
     example `192.168.1.20:8271`
   - **Additional application settings → TLS → No TLS Verify:** on (Cloudflare
     doesn't know your local CA)

   With a `config.yml` instead:

   ```yaml
   ingress:
     - hostname: voice.example.com
       service: https://192.168.1.20:8271
       originRequest:
         noTLSVerify: true
   ```

**Keep using the LAN at home.** List both addresses in `VK_URLS`, for example
`VK_URLS=https://192.168.1.20:8272,https://voice.example.com`: clients then
connect over the LAN when they can and through the tunnel otherwise, and the
web UI moves to the LAN address on its own (README → "Several addresses").

With `VK_PROTOCOL=https`, the service must be **HTTPS**. Plain HTTP only returns
the redirect to HTTPS, which the browser follows back through Cloudflare, and
it loops (`ERR_TOO_MANY_REDIRECTS`). Use option A to send plain HTTP.

## Scenario 4: port forwarding (router, VPS, dynamic DNS)

Use this when a public name such as a DuckDNS name points at a router or at
another server, which forwards a port to this server. For example,
`https://myname.duckdns.org:43365` is forwarded to `192.168.1.20:8271`.

1. Forward the chosen public port to `HTTPS_PORT` on this server as a plain
   **TCP** forward. It must not decrypt the traffic.
2. Add the public name to `VK_DOMAIN` and run `docker compose up -d`. Without
   it, browsers get `ERR_SSL_PROTOCOL_ERROR`.
3. The certificate comes from the local CA, so trust it on each device as in
   scenario 1, using the public address:
   `curl -fsSLk https://myname.duckdns.org:43365/client/trust-ca.sh | sh -s -- https://myname.duckdns.org:43365`.

Let's Encrypt can't be used through a non-standard port (see scenario 2).

## Scenario 5: behind an existing reverse proxy (Traefik, nginx, …)

Use this when another proxy already owns ports 80/443 on the machine and
should publish Voice Keyboard under its own domain and certificate.

1. Give Voice Keyboard any free port, for example `HTTPS_PORT=8271`, and add
   the proxy's public name to `VK_DOMAIN`.
2. Point the proxy at `https://127.0.0.1:8271` (or the server's LAN IP), with
   upstream certificate verification off. Pass the original `Host` header
   through, and allow WebSocket upgrades (`/v1/stream`, `/v1/events`,
   `/v1/keyboard`).
   - nginx: `proxy_pass https://127.0.0.1:8271; proxy_ssl_verify off;
     proxy_set_header Host $host; proxy_http_version 1.1;
     proxy_set_header Upgrade $http_upgrade; proxy_set_header Connection "upgrade";`
   - Traefik: a service with `url: https://127.0.0.1:8271` and a
     `serversTransport` with `insecureSkipVerify: true`.

## Changing names, ports or servers

- **New name or IP:** add it to `VK_DOMAIN` and run `docker compose up -d`.
  Caddy issues a certificate for it at once. Devices that already trust the
  local CA need nothing more.
- **New port:** change `HTTPS_PORT` and run `docker compose up -d`. Update
  tunnels, forwards and bookmarks.
- **Back up `backend/volumes/caddy`.** It holds the local CA. If you lose it
  (or move to a new server without it), Caddy creates a new CA, and every
  device has to trust the new one. Copy that folder along when you move the
  server.

## Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `ERR_SSL_PROTOCOL_ERROR`, "sent an invalid response" | The name in the address isn't in `VK_DOMAIN` | Add it, run `docker compose up -d` |
| "Not secure", `NET::ERR_CERT_AUTHORITY_INVALID` | This device doesn't trust the local CA | Trust it (scenario 1, step 3), restart the browser |
| `ERR_TOO_MANY_REDIRECTS` | A tunnel or proxy sends plain HTTP while `VK_PROTOCOL=https` | Use `VK_PROTOCOL=both` and point it at `HTTP_PORT`, or configure it for HTTPS with verification off (scenario 3/5) |
| Microphone button shows "needs HTTPS" | Page opened over HTTP, or the certificate isn't trusted | Open the `https://` address; trust the CA |
| Let's Encrypt certificate never arrives | Port 443 isn't reachable from the internet, or DNS points elsewhere | Check forwarding and DNS; see `docker compose logs https` |
| Worked before, "Not secure" after moving the server | New local CA (the `backend/volumes/caddy` folder wasn't kept) | Restore the folder, or trust the new CA on each device |

To see which certificate the server returns for a name:

```bash
echo | openssl s_client -connect <server-ip>:<HTTPS_PORT> -servername <name> 2>/dev/null | grep -E "subject=|issuer="
docker compose logs https | grep -i -E "certificate|error"
```
