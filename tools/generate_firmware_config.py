#!/usr/bin/env python3
"""Generate the ignored ESP32 firmware_config.h from esp32/.env."""

import os
from pathlib import Path

root = Path(__file__).resolve().parent.parent
out = root / "esp32" / "firmware_config.h"

def value(name, default=""):
    return os.environ.get(name, default).replace('\\', '\\\\').replace('"', '\\"')

out.write_text(
    "#pragma once\n\n"
    "// Generated file. Do not commit credentials.\n"
    f'#define VK_DEFAULT_WIFI_NAME "{value("VK_DEFAULT_WIFI_NAME")}"\n'
    f'#define VK_DEFAULT_WIFI_PASSWORD "{value("VK_DEFAULT_WIFI_PASSWORD")}"\n'
    f'#define VK_DEFAULT_SERVER_URL "{value("VK_DEFAULT_SERVER_URL")}"\n'
    f'#define VK_DEFAULT_API_KEY "{value("VK_DEFAULT_API_KEY", "change-me")}"\n'
    f'#define VK_DEFAULT_WEB_PASSWORD "{value("VK_DEFAULT_WEB_PASSWORD", "change-me")}"\n'
)
print(f"Generated {out}")
