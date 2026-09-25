#!/bin/sh
# One-command setup: ./setup.sh
#
# Creates the settings files on first run (random admin password and session
# secret, GPU or CPU, this machine's names and a free HTTPS port), starts the
# server with HTTPS, waits until it is ready, and prints how to open it and
# how to trust its certificate on each device. Safe to run again: existing
# settings are kept, and it just redeploys.
set -eu
cd "$(dirname "$0")"

say() { printf '%s\n' "$*"; }
rand() { LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c "$1"; }
# set_env FILE KEY VALUE: replace or add KEY=VALUE, keeping everything else.
set_env() {
  tmp="$1.tmp"
  grep -v "^$2=" "$1" >"$tmp" || true
  printf '%s=%s\n' "$2" "$3" >>"$tmp"
  mv "$tmp" "$1"
}
get_env() { sed -n "s/^$2=//p" "$1" | tail -1; }
port_free() {
  if command -v ss >/dev/null 2>&1; then ! ss -ltnH "( sport = :$1 )" | grep -q .
  else ! netstat -an 2>/dev/null | grep -Eq "[.:]$1 .*LISTEN"; fi
}

command -v docker >/dev/null 2>&1 || { say "Docker is required: https://docs.docker.com/engine/install/"; exit 1; }
docker compose version >/dev/null 2>&1 || { say "Docker Compose v2 is required."; exit 1; }

# --- Compose settings (.env): where Whisper runs, HTTPS names and ports
if [ ! -f .env ]; then
  cp .env.example .env
  if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1 && docker info 2>/dev/null | grep -qi nvidia; then
    profile=gpu
  else
    profile=cpu
  fi
  ip=$(hostname -I 2>/dev/null | awk '{print $1}')
  [ -n "$ip" ] || ip=$(ipconfig getifaddr en0 2>/dev/null || true)
  host=$(hostname -s 2>/dev/null || hostname)
  names="localhost"
  [ -n "$host" ] && names="$host, $names"
  [ -n "$ip" ] && names="$ip, $names"
  https_port=443; for p in 443 8272 9443; do if port_free "$p"; then https_port=$p; break; fi; done
  http_port=80; for p in 80 8271 8080; do if port_free "$p"; then http_port=$p; break; fi; done
  set_env .env COMPOSE_PROFILES "$profile"
  set_env .env VK_DOMAIN "$names"
  set_env .env VK_DEFAULT_SNI "${ip:-localhost}"
  set_env .env HTTPS_PORT "$https_port"
  set_env .env HTTP_PORT "$http_port"
  say "Created .env: Whisper on $profile; HTTPS for $names on port $https_port."
fi

# --- App settings (backend/.env): admin password and session secret
if [ ! -f backend/.env ]; then
  cp backend/.env.example backend/.env
  password=$(rand 16)
  set_env backend/.env WEB_PASSWORD "$password"
  set_env backend/.env SESSION_SECRET "$(rand 48)"
  say "Created backend/.env with a random admin password (shown at the end)."
fi

https_port=$(get_env .env HTTPS_PORT); https_port=${https_port:-443}
server=$(get_env .env VK_DEFAULT_SNI); server=${server:-localhost}
suffix() { [ "$1" = "$2" ] && echo "" || echo ":$1"; }
https_url="https://$server$(suffix "$https_port" 443)"
http_port=$(get_env .env HTTP_PORT); http_port=${http_port:-80}
http_url="http://$server$(suffix "$http_port" 80)"
protocol=$(get_env .env VK_PROTOCOL); protocol=${protocol:-https}
check_url=$https_url; [ "$protocol" = http ] && check_url=$http_url

say "Starting (the first start downloads the Whisper model, which can take several minutes)..."
docker compose up -d --build

printf 'Waiting for the server'
ready=""
for _ in $(seq 1 180); do
  if curl -fsSk "$check_url/health" >/dev/null 2>&1; then ready=1; break; fi
  printf '.'; sleep 5
done
say ""
[ -n "$ready" ] || { say "Not ready after 15 minutes; check: docker compose logs -f"; exit 1; }

say ""
case "$protocol" in
  http) say "Voice Keyboard is running at $http_url (plain HTTP: put a tunnel or proxy with HTTPS in front)" ;;
  both) say "Voice Keyboard is running at $https_url and, without HTTPS, at $http_url" ;;
  *) say "Voice Keyboard is running at $https_url" ;;
esac
[ -n "${password:-}" ] && say "Sign in as admin with password: $password   (change it in Settings → Account)"
tls=$(get_env .env VK_TLS)
if [ "${tls:-internal}" = internal ] && [ "$protocol" != http ]; then
  say ""
  say "Browsers allow the microphone only over HTTPS they trust. This server signs its"
  say "certificate with its own local CA; trust it once on each device you use"
  say "(the desktop client's install command in Settings → Clients does this for you):"
  say "  macOS / Linux:  curl -fsSLk $https_url/client/trust-ca.sh | sh -s -- $https_url"
  say "  Windows:        powershell -c \"[Net.ServicePointManager]::ServerCertificateValidationCallback={\$true}; irm $https_url/client/trust-ca.ps1 | iex\""
  say "  iPhone / iPad:  open $https_url/ca.crt (accept the warning once), install the profile,"
  say "                  then enable it in Settings → General → About → Certificate Trust Settings"
  say "  Android:        download $https_url/ca.crt, then Settings → Security → Install a certificate → CA"
fi
