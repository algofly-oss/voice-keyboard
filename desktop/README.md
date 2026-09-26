# Desktop client

`vkeyboard` is one self-contained program (about 6 MB) that types dictated
text into the focused window. It needs no runtime (such as Python or .NET),
packages, or administrator rights.

| OS | CPUs | Types with | Starts at login via |
| --- | --- | --- | --- |
| macOS | Apple Silicon, Intel | CoreGraphics key events (Unicode) | LaunchAgent |
| Windows | x86-64, ARM64 | `SendInput` (Unicode) | `HKCU\…\Run` |
| Linux X11 | x86-64, ARM64 | XTest (Unicode, any layout) | XDG autostart |
| Linux Wayland | x86-64, ARM64 | `/dev/uinput` (US layout, ASCII only) | XDG autostart |

## Install

Copy the command from the web app: **Settings → Clients**. It downloads the
binary for this OS and CPU, pairs it, and starts it in the background:

```bash
curl -fsSL https://host/client/install.sh | sh -s -- --server https://host --token …   # macOS, Linux
```
```powershell
& ([scriptblock]::Create((irm 'https://host/client/install.ps1'))) -Server 'https://host' -Token '…'  # Windows
```

Running the command again upgrades the client in place.

## Use

```text
vkeyboard status      running? connected? selected to type? permission problems?
vkeyboard logs        recent log lines, then follow live (-n 100, --no-follow)
vkeyboard stop        stop, and stay off after restarts
vkeyboard start       start in the background, and at every login
vkeyboard config      show or change typing: --method type|paste --delay-ms N|default
vkeyboard type-test   type a test string after 3 seconds
vkeyboard uninstall   remove settings and the login item
```

`start` and `stop` return immediately; the client itself runs detached from
the terminal. After `start` it comes back automatically after every restart or
login until you run `stop`. It never gives up on the server: every failure
(network change, server restart, a display that isn't ready yet at login) is
retried with back-off.

**Several server addresses** (`VK_URLS` on the server, e.g. the LAN address
and a Cloudflare name): the client learns them on connect, keeps them in its
config, and connects to the fastest local address that answers with the same
server id, else to a public one. While on a public address it re-checks the
local ones every 15 s and moves over as soon as one answers. `status` lists
the addresses and the one in use. Local certificates are checked against the
server's CA (sent by the server), so no `--trust-local-ca` is needed for this.

Each client appears in the web UI under **Settings → Clients** with its
name, OS/CPU, and version. Only the active one types, and `status` shows
whether this client is it. Logs roll over at 1 MB and keep three files
(`vkeyboard.log`, `.1`, `.2`) in the config directory shown by `status`.

**Platform notes**

- **macOS:** the first start opens the Accessibility prompt. Allow
  `vkeyboard` there; typing begins as soon as it is allowed, with no
  restart. The binary is ad-hoc signed, so after an upgrade macOS may ask
  again.
- **Linux on Wayland:** XTest only reaches X11 (XWayland) apps. To type into
  all apps, allow the virtual keyboard device once (the installer prints
  these commands):

  ```bash
  echo 'KERNEL=="uinput", TAG+="uaccess"' | sudo tee /etc/udev/rules.d/60-vkeyboard.rules
  echo uinput | sudo tee /etc/modules-load.d/vkeyboard.conf
  sudo modprobe uinput && sudo udevadm control --reload && sudo udevadm trigger --name-match=uinput
  ```

**Typing method and speed** (`vkeyboard config`, applied immediately):

- `--method type` (default) sends key events. Windows sends each piece in one
  call and X11 types with no delay. macOS and Wayland pause 2 ms per character
  by default, because some apps drop characters that arrive faster.
  `--delay-ms N` changes that pause (`0` = as fast as possible).
- `--method paste` pastes pieces of 80+ characters through the clipboard and
  restores it right after (macOS, Windows). It only does so when the clipboard
  holds plain text, so an image, files or formatting you copied are never
  lost; otherwise it types. Windows keeps the text out of clipboard history.
  Linux always types: terminals there paste with Ctrl+Shift+V, and XTest is
  already instant.

`vkeyboard logs` shows each piece as it is received and typed, with its length
and timing (not the text itself).

- **Windows:** keys cannot be sent to windows running as administrator unless
  the client runs elevated as well (a Windows security rule, UIPI).

## Build

Pure Go with no cgo, so every target cross-compiles from any machine. OS
libraries are loaded at run time with [purego](https://github.com/ebitengine/purego).
The only other dependency is [coder/websocket](https://github.com/coder/websocket).

```bash
cd desktop
CGO_ENABLED=0 GOOS=darwin GOARCH=arm64 go build -trimpath -ldflags "-s -w" -o vkeyboard .
# Windows: add -H windowsgui so the login item has no console window
```

`tools/release.sh <version>` builds all six and attaches them to the GitHub
release, which the backend mirrors and serves at `/client/vkeyboard-<os>-<arch>`.
