package main

import (
	"encoding/json"
	"errors"
	"io"
	"log"
	"os"
	"path/filepath"
	"time"
)

// config is written by enroll and read by every other command.
type config struct {
	Server     string `json:"server"`
	Client     string `json:"client"`
	Room       string `json:"room"`
	Credential string `json:"credential"`
}

// state is written by the running client so status/stop can find it.
type state struct {
	PID       int       `json:"pid"`
	Connected bool      `json:"connected"`
	Since     time.Time `json:"since"`
	LastError string    `json:"lastError,omitempty"`
	Warning   string    `json:"warning,omitempty"` // e.g. missing typing permission
}

func (s state) running() bool { return s.PID > 0 && processAlive(s.PID) }

func configDir() string {
	dir, err := os.UserConfigDir()
	if err != nil {
		dir = os.TempDir()
	}
	return filepath.Join(dir, "voice-keyboard")
}

func configPath() string { return filepath.Join(configDir(), "config.json") }
func statePath() string  { return filepath.Join(configDir(), "state.json") }
func logPath() string    { return filepath.Join(configDir(), "voice-keyboard.log") }

func loadConfig() (config, error) {
	var cfg config
	data, err := os.ReadFile(configPath())
	if errors.Is(err, os.ErrNotExist) {
		return cfg, errors.New("not paired yet; run the install command from the web app (Settings → Computers)")
	}
	if err == nil {
		err = json.Unmarshal(data, &cfg)
	}
	if cfg.Room == "" {
		cfg.Room = "voice-keyboard"
	}
	return cfg, err
}

func saveConfig(cfg config) error { return writeJSON(configPath(), cfg) }

func readState() state {
	var s state
	if data, err := os.ReadFile(statePath()); err == nil {
		_ = json.Unmarshal(data, &s)
	}
	return s
}

func writeState(s state) { _ = writeJSON(statePath(), s) }
func clearState()        { _ = os.Remove(statePath()) }

// writeJSON replaces the file atomically; the credential makes it private.
func writeJSON(path string, v any) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return err
	}
	data, err := json.MarshalIndent(v, "", "  ")
	if err != nil {
		return err
	}
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, append(data, '\n'), 0o600); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

// redirectLogIfDetached sends logs to the log file when there is no terminal
// (login item, background start), trimming it once it grows past 1 MB.
func redirectLogIfDetached() func() {
	if isTerminal(os.Stderr) {
		return func() {}
	}
	if info, err := os.Stat(logPath()); err == nil && info.Size() > 1<<20 {
		_ = os.Rename(logPath(), logPath()+".old")
	}
	_ = os.MkdirAll(configDir(), 0o700)
	f, err := os.OpenFile(logPath(), os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		return func() {}
	}
	log.SetOutput(io.MultiWriter(f))
	return func() { f.Close() }
}

func isTerminal(f *os.File) bool {
	info, err := f.Stat()
	return err == nil && info.Mode()&os.ModeCharDevice != 0
}
