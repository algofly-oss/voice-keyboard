//go:build darwin || linux

package main

import (
	"os"
	"os/exec"
	"syscall"
)

func pidSelf() int { return os.Getpid() }

func processAlive(pid int) bool { return syscall.Kill(pid, 0) == nil }

func stopProcess(pid int) error { return syscall.Kill(pid, syscall.SIGTERM) }

// startDetached launches `vkeyboard run` in its own session so it
// outlives the terminal that started it.
func startDetached() error {
	exe, err := os.Executable()
	if err != nil {
		return err
	}
	return startDetachedExe(exe)
}

// startDetachedExe starts `exe run` from a given file (after an update the
// running program's own path may point at the replaced file), with extra env.
func startDetachedExe(exe string, env ...string) error {
	cmd := exec.Command(exe, "run")
	cmd.Env = append(os.Environ(), env...)
	cmd.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	if err := cmd.Start(); err != nil {
		return err
	}
	return cmd.Process.Release()
}

func removeHint(exe string) string { return "rm " + exe }
