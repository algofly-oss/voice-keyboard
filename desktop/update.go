package main

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strconv"
	"strings"
	"sync"
	"time"
)

// Self-update. The server offers a newer build ({"type":"update",…}) when the
// client connects, after a new release, or when the user presses Update in the
// web app. The client downloads it from the same server, checks its size and
// SHA-256, runs it once to see it works here, swaps it in and restarts.
type updateOffer struct {
	Version, URL, SHA256 string
	Size                 int64
	Manual               bool // from the Update button: also reinstalls, and ignores recent failures
}

var (
	updating       sync.Mutex
	updateFailedAt = map[string]time.Time{} // an automatic retry of a failed version waits an hour
)

func handleUpdate(server, ca string, u updateOffer) {
	if !u.Manual && (u.Version == version || version == "dev") {
		return // current, or a development build that must not be replaced by a release
	}
	if t, failed := updateFailedAt[u.Version]; failed && !u.Manual && time.Since(t) < time.Hour {
		return
	}
	if !updating.TryLock() {
		return // an update is already running
	}
	go func() {
		defer updating.Unlock()
		log.Printf("updating %s → %s", version, u.Version)
		if err := selfUpdate(server, ca, u); err != nil {
			updateFailedAt[u.Version] = time.Now()
			reportError("update to %s failed: %v", u.Version, err)
		}
	}()
}

func selfUpdate(server, ca string, u updateOffer) error {
	if !strings.HasPrefix(u.URL, "/client/") || len(u.SHA256) != 64 || u.Size <= 0 {
		return errors.New("malformed update offer")
	}
	exe, err := os.Executable()
	if err == nil {
		exe, err = filepath.EvalSymlinks(exe)
	}
	if err != nil {
		return err
	}
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Minute)
	defer cancel()
	req, err := http.NewRequestWithContext(ctx, http.MethodGet, strings.TrimRight(server, "/")+u.URL, nil)
	if err != nil {
		return err
	}
	resp, err := httpClient(ca, server).Do(req)
	if err != nil {
		return err
	}
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		return fmt.Errorf("download: HTTP %d", resp.StatusCode)
	}
	next := exe + ".new"
	f, err := os.OpenFile(next, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0o755)
	if err != nil {
		return err
	}
	defer os.Remove(next) // gone after a successful swap anyway
	hash := sha256.New()
	n, err := io.Copy(io.MultiWriter(f, hash), io.LimitReader(resp.Body, u.Size+1))
	if closeErr := f.Close(); err == nil {
		err = closeErr
	}
	if err != nil {
		return fmt.Errorf("download: %w", err)
	}
	if n != u.Size || hex.EncodeToString(hash.Sum(nil)) != u.SHA256 {
		return fmt.Errorf("download is damaged (%d of %d bytes, or the checksum differs)", n, u.Size)
	}
	// It must start on this computer and be the version offered.
	out, err := exec.CommandContext(ctx, next, "version").Output()
	if got := strings.TrimSpace(string(out)); err != nil || got != u.Version {
		return fmt.Errorf("the new build did not run here (%v, version %q)", err, got)
	}
	if err := swapBinary(exe, next); err != nil {
		return fmt.Errorf("could not replace %s: %w", exe, err)
	}
	log.Printf("updated to %s; restarting", u.Version)
	restartAfterUpdate(exe)
	return nil
}

// swapBinary puts next in place of exe. A running program's file can be
// replaced on macOS and Linux; Windows only allows renaming it out of the way.
func swapBinary(exe, next string) error {
	if runtime.GOOS != "windows" {
		return os.Rename(next, exe)
	}
	old := exe + ".old"
	_ = os.Remove(old)
	if err := os.Rename(exe, old); err != nil {
		return err
	}
	if err := os.Rename(next, exe); err != nil {
		_ = os.Rename(old, exe)
		return err
	}
	return nil
}

// restartAfterUpdate hands over to the new build and exits.
func restartAfterUpdate(exe string) {
	clearState()
	// Under launchd (the macOS login item), a failed exit makes launchd start it again.
	if underLaunchd() {
		os.Exit(75)
	}
	if err := startDetachedExe(exe, updatedFromEnv+"="+strconv.Itoa(os.Getpid())); err != nil {
		reportError("could not start the updated client: %v", err)
		return // keep running the old build
	}
	os.Exit(0)
}

// The new process waits for the old one to exit before its single-instance check.
const updatedFromEnv = "VKEYBOARD_UPDATED_FROM"

func waitForPreviousProcess() {
	pid, err := strconv.Atoi(os.Getenv(updatedFromEnv))
	if err != nil {
		return
	}
	for deadline := time.Now().Add(10 * time.Second); processAlive(pid) && time.Now().Before(deadline); {
		time.Sleep(100 * time.Millisecond)
	}
	os.Unsetenv(updatedFromEnv)
}

// removeLeftovers deletes the previous build Windows kept renamed during an update.
func removeLeftovers() {
	if exe, err := os.Executable(); err == nil {
		_ = os.Remove(exe + ".old")
		_ = os.Remove(exe + ".new")
	}
}
