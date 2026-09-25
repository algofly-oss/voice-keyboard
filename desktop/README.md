# Desktop client

`voice-keyboard` is one self-contained program (about 6 MB) that types dictated
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
voice-keyboard status      running? connected? selected to type? permission problems?
voice-keyboard logs        recent log lines, then follow live (-n 100, --no-follow)
voice-keyboard stop        stop, and stay off after restarts
voice-keyboard start       start in the background, and at every login
voice-keyboard type-test   type a test string after 3 seconds
voice-keyboard uninstall   remove settings and the login item
```

`start` and `stop` return immediately; the client itself runs detached from
the terminal. After `start` it comes back automatically after every restart or
login until you run `stop`. It never gives up on the server: every failure
(network change, server restart, a display that isn't ready yet at login) is
retried with back-off.

Each client appears in the web UI under **Settings → Clients** with its
name, OS/CPU, and version. Only the active one types, and `status` shows
whether this client is it. Logs roll over at 1 MB and keep three files
(`voice-keyboard.log`, `.1`, `.2`) in the config directory shown by `status`.

**Platform notes**

- **macOS:** the first start opens the Accessibility prompt. Allow
  `voice-keyboard` there; typing begins as soon as it is allowed, with no
  restart. The binary is ad-hoc signed, so after an upgrade macOS may ask
  again.
- **Linux on Wayland:** XTest only reaches X11 (XWayland) apps. To type into
  all apps, allow the virtual keyboard device once (the installer prints
  these commands):

  ```bash
  echo 'KERNEL=="uinput", TAG+="uaccess"' | sudo tee /etc/udev/rules.d/60-voice-keyboard.rules
  echo uinput | sudo tee /etc/modules-load.d/voice-keyboard.conf
  sudo modprobe uinput && sudo udevadm control --reload && sudo udevadm trigger --name-match=uinput
  ```

- **Windows:** keys cannot be sent to windows running as administrator unless
  the client runs elevated as well (a Windows security rule, UIPI).

## Build

Pure Go with no cgo, so every target cross-compiles from any machine. OS
libraries are loaded at run time with [purego](https://github.com/ebitengine/purego).
The only other dependency is [coder/websocket](https://github.com/coder/websocket).

```bash
cd desktop
CGO_ENABLED=0 GOOS=darwin GOARCH=arm64 go build -trimpath -ldflags "-s -w" -o voice-keyboard .
# Windows: add -H windowsgui so the login item has no console window
```

`tools/release.sh <version>` builds all six and attaches them to the GitHub
release, which the backend mirrors and serves at `/client/voice-keyboard-<os>-<arch>`.
