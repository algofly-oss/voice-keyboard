package main

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
)

// A per-user LaunchAgent starts the client at login and restarts it if it
// crashes. launchd, not the terminal, is its parent, so macOS attributes the
// Accessibility permission to voice-keyboard itself.
const launchLabel = "ai.algofly.voicekeyboard"

func launchAgentPath() string {
	home, _ := os.UserHomeDir()
	return filepath.Join(home, "Library", "LaunchAgents", launchLabel+".plist")
}

func launchDomain() string { return fmt.Sprintf("gui/%d", os.Getuid()) }

func enableAutostart() error {
	exe, err := os.Executable()
	if err != nil {
		return err
	}
	plist := fmt.Sprintf(`<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
	<key>Label</key><string>%s</string>
	<key>ProgramArguments</key><array><string>%s</string><string>run</string></array>
	<key>RunAtLoad</key><true/>
	<key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
	<key>ProcessType</key><string>Interactive</string>
</dict>
</plist>
`, launchLabel, xmlEscape(exe))
	if err := os.MkdirAll(filepath.Dir(launchAgentPath()), 0o755); err != nil {
		return err
	}
	if err := os.WriteFile(launchAgentPath(), []byte(plist), 0o644); err != nil {
		return err
	}
	_ = exec.Command("launchctl", "bootout", launchDomain()+"/"+launchLabel).Run()
	if out, err := exec.Command("launchctl", "bootstrap", launchDomain(), launchAgentPath()).CombinedOutput(); err != nil {
		return fmt.Errorf("launchctl bootstrap: %v: %s", err, out)
	}
	return nil
}

func disableAutostart() error {
	_ = exec.Command("launchctl", "bootout", launchDomain()+"/"+launchLabel).Run()
	if err := os.Remove(launchAgentPath()); err != nil && !os.IsNotExist(err) {
		return err
	}
	return nil
}

func autostartEnabled() bool {
	_, err := os.Stat(launchAgentPath())
	return err == nil
}

func xmlEscape(s string) string {
	out := ""
	for _, r := range s {
		switch r {
		case '&':
			out += "&amp;"
		case '<':
			out += "&lt;"
		case '>':
			out += "&gt;"
		default:
			out += string(r)
		}
	}
	return out
}
