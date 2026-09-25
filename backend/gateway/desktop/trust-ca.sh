#!/bin/sh
# Trusts this server's local certificate authority, so browsers accept its
# HTTPS certificate (and allow the microphone) without warnings.
#   curl -fsSL http://<server>/client/trust-ca.sh | sh -s -- http://<server>
set -eu
server=${1:?usage: trust-ca.sh http://<server>[:port]}
cert=$(mktemp)
curl -fsSL "$server/ca.crt" -o "$cert"
grep -q "BEGIN CERTIFICATE" "$cert" || { echo "No CA certificate at $server/ca.crt" >&2; exit 1; }
case "$(uname -s)" in
  Darwin)
    echo "Adding the Voice Keyboard CA to the macOS System keychain (asks for your password)..."
    sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain "$cert" ;;
  Linux)
    if [ -d /usr/local/share/ca-certificates ]; then
      sudo cp "$cert" /usr/local/share/ca-certificates/vkeyboard-ca.crt && sudo update-ca-certificates
    elif [ -d /etc/pki/ca-trust/source/anchors ]; then
      sudo cp "$cert" /etc/pki/ca-trust/source/anchors/vkeyboard-ca.crt && sudo update-ca-trust
    else
      echo "Unknown Linux certificate store; add $cert to it manually." >&2
    fi
    # Chrome and Firefox on Linux use their own NSS store.
    if command -v certutil >/dev/null 2>&1; then
      mkdir -p "$HOME/.pki/nssdb"
      certutil -d "sql:$HOME/.pki/nssdb" -A -t "C,," -n "Voice Keyboard CA" -i "$cert"
    else
      echo "For Chrome/Firefox, also install libnss3-tools (certutil) and run this again."
    fi ;;
  *) echo "Unsupported system; install $cert as a trusted root manually." >&2; exit 1 ;;
esac
rm -f "$cert"
echo "Done. Restart the browser, then open the https:// address."
