package main

import (
	"errors"
	"flag"
	"fmt"
	"strconv"
	"time"
)

// configCommand shows or changes how text is entered, then restarts the
// running client so the change applies at once.
//
//	vkeyboard config --method paste --delay-ms 0
func configCommand(args []string) error {
	cfg, err := loadConfig()
	if err != nil {
		return err
	}
	flags := flag.NewFlagSet("config", flag.ContinueOnError)
	method := flags.String("method", "", "type (key events, default) or paste (clipboard, for long pieces; restored afterwards)")
	delay := flags.String("delay-ms", "", "pause between typed characters in milliseconds, or \"default\"")
	if err := flags.Parse(args); err != nil {
		return err
	}
	changed := false
	switch *method {
	case "":
	case "type", "paste":
		cfg.Method, changed = *method, true
		if *method == "type" {
			cfg.Method = ""
		}
	default:
		return errors.New("--method must be type or paste")
	}
	switch *delay {
	case "":
	case "default":
		cfg.DelayMs, changed = nil, true
	default:
		ms, err := strconv.Atoi(*delay)
		if err != nil || ms < 0 || ms > 1000 {
			return errors.New("--delay-ms must be 0–1000 or default")
		}
		cfg.DelayMs, changed = &ms, true
	}
	if changed {
		if err := saveConfig(cfg); err != nil {
			return err
		}
		if err := restartIfRunning(); err != nil {
			return err
		}
	}
	fmt.Printf("Typing: %s\n", typingDescription(cfg))
	if !changed {
		fmt.Println("Change it with: vkeyboard config --method type|paste --delay-ms N|default")
	}
	return nil
}

func typingDescription(cfg config) string {
	method := "type (key events)"
	if cfg.Method == "paste" {
		method = fmt.Sprintf("paste (pieces of %d+ characters via the clipboard, restored after)", pasteThreshold)
	}
	delay := "platform default delay"
	if cfg.DelayMs != nil {
		delay = fmt.Sprintf("%d ms between characters", *cfg.DelayMs)
	}
	return method + ", " + delay
}

// restartIfRunning applies new settings to the background client.
func restartIfRunning() error {
	s := readState()
	if !s.running() {
		return nil
	}
	if err := stopProcess(s.PID); err != nil {
		return err
	}
	_ = waitForState(func(s state) bool { return !s.running() }, 5*time.Second)
	if !autostartEnabled() {
		return startDetached()
	}
	if err := enableAutostart(); err != nil {
		return err
	}
	fmt.Println("Restarted the running client with the new setting.")
	return nil
}
