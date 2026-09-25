#!/usr/bin/env python3
"""Register iPhones with Apple for Ad Hoc builds (used by the iOS Ad Hoc workflow).

Authenticates with an App Store Connect API key:
  ASC_KEY_ID, ASC_ISSUER_ID, and ASC_KEY_PATH (the .p8 file).

  asc_devices.py register <udid> <name>   # no-op if already registered
  asc_devices.py list                     # enabled iOS devices, one UDID per line
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request

import jwt  # PyJWT with cryptography

API = "https://api.appstoreconnect.apple.com/v1"


def token() -> str:
    now = int(time.time())
    key = open(os.environ["ASC_KEY_PATH"]).read()
    return jwt.encode({"iss": os.environ["ASC_ISSUER_ID"], "iat": now, "exp": now + 900, "aud": "appstoreconnect-v1"},
                      key, algorithm="ES256", headers={"kid": os.environ["ASC_KEY_ID"], "typ": "JWT"})


def call(method: str, path: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body else None,
                                     headers={"Authorization": f"Bearer {token()}", "Content-Type": "application/json"})
    with urllib.request.urlopen(request) as response:
        return json.load(response)


def devices() -> list[dict]:
    found, path = [], "/devices?filter[platform]=IOS&limit=200"
    while path:
        page = call("GET", path)
        found += page["data"]
        path = page.get("links", {}).get("next", "").removeprefix(API) or None
    return found


def main():
    if sys.argv[1:2] == ["list"]:
        for device in devices():
            if device["attributes"]["status"] == "ENABLED":
                print(device["attributes"]["udid"])
    elif sys.argv[1:2] == ["register"] and len(sys.argv) == 4:
        udid, name = sys.argv[2], sys.argv[3]
        if any(d["attributes"]["udid"].lower() == udid.lower() for d in devices()):
            print(f"{name} is already registered")
            return
        try:
            call("POST", "/devices", {"data": {"type": "devices",
                                               "attributes": {"name": name, "udid": udid, "platform": "IOS"}}})
        except urllib.error.HTTPError as e:
            sys.exit(f"Apple rejected the device: {e.read().decode()}")
        print(f"Registered {name}")
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
