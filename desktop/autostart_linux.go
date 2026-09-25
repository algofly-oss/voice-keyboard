package main

import (
	"fmt"
	"os"
	"path/filepath"
)

// An XDG autostart entry works in GNOME, KDE, XFCE and most other desktops,
// and runs inside the graphical session, so DISPLAY/WAYLAND_DISPLAY are set.
func autostartPath() string {
	dir := os.Getenv("XDG_CONFIG_HOME")
	if dir == "" {
		home, _ := os.UserHomeDir()
		dir = filepath.Join(home, ".config")
	}
	return filepath.Join(dir, "autostart", "voice-keyboard.desktop")
}

func enableAutostart() error {
	exe, err := os.Executable()
	if err != nil {
		return err
	}
	entry := fmt.Sprintf(`[Desktop Entry]
Type=Application
Name=Voice Keyboard
Comment=Types text dictated in the Voice Keyboard web app
Exec="%s" run
Terminal=false
NoDisplay=true
X-GNOME-Autostart-enabled=true
`, exe)
	if err := os.MkdirAll(filepath.Dir(autostartPath()), 0o755); err != nil {
		return err
	}
	if err := os.WriteFile(autostartPath(), []byte(entry), 0o644); err != nil {
		return err
	}
	if readState().running() {
		return nil
	}
	return startDetached()
}

func disableAutostart() error {
	if err := os.Remove(autostartPath()); err != nil && !os.IsNotExist(err) {
		return err
	}
	return nil
}

func autostartEnabled() bool {
	_, err := os.Stat(autostartPath())
	return err == nil
}
