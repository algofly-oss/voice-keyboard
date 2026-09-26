package main

import (
	"fmt"
	"os"
	"os/exec"
	"syscall"
)

// The per-user Run key starts the client at login; reg.exe keeps this free of
// extra dependencies.
const runKey = `HKCU\Software\Microsoft\Windows\CurrentVersion\Run`

func reg(args ...string) ([]byte, error) {
	cmd := exec.Command("reg.exe", args...)
	cmd.SysProcAttr = &syscall.SysProcAttr{HideWindow: true}
	return cmd.CombinedOutput()
}

func enableAutostart() error {
	exe, err := os.Executable()
	if err != nil {
		return err
	}
	if out, err := reg("add", runKey, "/v", "VKeyboard", "/t", "REG_SZ", "/d", fmt.Sprintf(`"%s" run`, exe), "/f"); err != nil {
		return fmt.Errorf("reg add: %v: %s", err, out)
	}
	if readState().running() {
		return nil
	}
	return startDetached()
}

// removeOldAutostart deletes the Run entry of releases named voice-keyboard.
func removeOldAutostart() {
	_, _ = reg("delete", runKey, "/v", "VoiceKeyboard", "/f")
}

func disableAutostart() error {
	if autostartEnabled() {
		if out, err := reg("delete", runKey, "/v", "VKeyboard", "/f"); err != nil {
			return fmt.Errorf("reg delete: %v: %s", err, out)
		}
	}
	return nil
}

func autostartEnabled() bool {
	_, err := reg("query", runKey, "/v", "VKeyboard")
	return err == nil
}

func underLaunchd() bool { return false }
