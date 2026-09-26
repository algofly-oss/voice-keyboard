package main

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"os"
	"path/filepath"
	"sync"
	"time"
)

// config is written by enroll and read by every other command.
type config struct {
	Server     string `json:"server"`
	Client     string `json:"client"`
	Credential string `json:"credential"`
	// From the server on every connection (servers.go): its other addresses,
	// its instance id and its local CA.
	Servers  []string `json:"servers,omitempty"`
	Instance string   `json:"instance,omitempty"`
	CA       string   `json:"ca,omitempty"`
	// Typing preferences, changed with `vkeyboard config`.
	Method  string `json:"method,omitempty"`        // "type" (default) or "paste"
	DelayMs *int   `json:"typingDelayMs,omitempty"` // nil: the platform default
}

// state is written by the running client so status/stop can find it.
type state struct {
	PID       int       `json:"pid"`
	Connected bool      `json:"connected"`
	Since     time.Time `json:"since"`
	LastError string    `json:"lastError,omitempty"`
	Warning   string    `json:"warning,omitempty"`  // e.g. missing typing permission
	Selected  *bool     `json:"selected,omitempty"` // whether the web app picked this computer to type
	Server    string    `json:"server,omitempty"`   // the address connected to
}

func (s state) running() bool { return s.PID > 0 && processAlive(s.PID) }

func configDir() string {
	dir, err := os.UserConfigDir()
	if err != nil {
		dir = os.TempDir()
	}
	return filepath.Join(dir, "vkeyboard")
}

func configPath() string { return filepath.Join(configDir(), "config.json") }

// oldConfigDir is where releases before 1.3 (named voice-keyboard) kept their files.
func oldConfigDir() string { return filepath.Join(filepath.Dir(configDir()), "voice-keyboard") }

// removeOldInstall retires a client installed under the old name: it stops
// that agent, removes its login item and deletes its settings (keeping the
// machine id, so the web app shows the same client, not a new one).
func removeOldInstall() {
	machineID()
	var old state
	if data, err := os.ReadFile(filepath.Join(oldConfigDir(), "state.json")); err == nil {
		_ = json.Unmarshal(data, &old)
	}
	if old.running() && old.PID != os.Getpid() {
		_ = stopProcess(old.PID)
	}
	removeOldAutostart()
	_ = os.RemoveAll(oldConfigDir())
}

// machineID identifies this computer across reinstalls, so pairing again
// updates the same entry in the web app instead of adding a duplicate.
func machineID() string {
	path := filepath.Join(configDir(), "machine-id")
	if data, err := os.ReadFile(path); err == nil && len(data) >= 16 {
		return string(data)
	}
	// Keep the id of an install made under the old "voice-keyboard" name.
	if data, err := os.ReadFile(filepath.Join(oldConfigDir(), "machine-id")); err == nil && len(data) >= 16 {
		_ = os.MkdirAll(configDir(), 0o700)
		_ = os.WriteFile(path, data, 0o600)
		return string(data)
	}
	buf := make([]byte, 16)
	_, _ = rand.Read(buf)
	id := hex.EncodeToString(buf)
	_ = os.MkdirAll(configDir(), 0o700)
	_ = os.WriteFile(path, []byte(id), 0o600)
	return id
}
func statePath() string { return filepath.Join(configDir(), "state.json") }
func logPath() string   { return filepath.Join(configDir(), "vkeyboard.log") }

func loadConfig() (config, error) {
	var cfg config
	data, err := os.ReadFile(configPath())
	if errors.Is(err, os.ErrNotExist) {
		return cfg, errors.New("not paired yet; run the install command from the web app (Settings → Clients)")
	}
	if err == nil {
		err = json.Unmarshal(data, &cfg)
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

// redirectLogIfDetached writes logs to a rolling log file (read by
// `vkeyboard logs`), and to the terminal when there is one. The file comes
// first so a missing console (Windows login item) cannot stop it being written.
func redirectLogIfDetached() func() {
	w := &rollingLog{path: logPath(), maxBytes: 1 << 20, keep: 3}
	log.SetOutput(io.MultiWriter(w, os.Stderr))
	return w.Close
}

// rollingLog caps disk use: the log rolls over at maxBytes into .1 … .keep-1,
// so at most keep × maxBytes (3 MB) is ever stored.
type rollingLog struct {
	mu             sync.Mutex
	path           string
	maxBytes, size int64
	keep           int
	f              *os.File
}

func (w *rollingLog) Write(p []byte) (int, error) {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.f == nil || w.size+int64(len(p)) > w.maxBytes {
		if err := w.roll(); err != nil {
			return 0, err
		}
	}
	n, err := w.f.Write(p)
	w.size += int64(n)
	return n, err
}

func (w *rollingLog) roll() error {
	if w.f != nil {
		w.f.Close()
		for i := w.keep - 1; i > 1; i-- {
			_ = os.Rename(fmt.Sprintf("%s.%d", w.path, i-1), fmt.Sprintf("%s.%d", w.path, i))
		}
		_ = os.Rename(w.path, w.path+".1")
	}
	_ = os.MkdirAll(filepath.Dir(w.path), 0o700)
	f, err := os.OpenFile(w.path, os.O_CREATE|os.O_APPEND|os.O_WRONLY, 0o600)
	if err != nil {
		return err
	}
	info, _ := f.Stat()
	w.f, w.size = f, 0
	if info != nil {
		w.size = info.Size()
	}
	return nil
}

func (w *rollingLog) Close() {
	w.mu.Lock()
	defer w.mu.Unlock()
	if w.f != nil {
		w.f.Close()
	}
}
