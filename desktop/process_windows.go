package main

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
)

var procAttachConsole = syscall.NewLazyDLL("kernel32.dll").NewProc("AttachConsole")

// The Windows build uses the GUI subsystem so the login item runs without a
// console window. When run from a terminal it attaches to that console so
// commands still print their output (voice-keyboard.cmd waits for it).
func init() {
	const attachParentProcess = ^uintptr(0) // ATTACH_PARENT_PROCESS, (DWORD)-1
	if r, _, _ := procAttachConsole.Call(attachParentProcess); r == 0 {
		return
	}
	if f, err := os.OpenFile("CONOUT$", os.O_WRONLY, 0); err == nil {
		os.Stdout, os.Stderr = f, f
	}
}

func pidSelf() int { return os.Getpid() }

func processAlive(pid int) bool {
	const queryLimitedInformation, stillActive = 0x1000, 259
	h, err := syscall.OpenProcess(queryLimitedInformation, false, uint32(pid))
	if err != nil {
		return false
	}
	defer syscall.CloseHandle(h)
	var code uint32
	return syscall.GetExitCodeProcess(h, &code) == nil && code == stillActive
}

func stopProcess(pid int) error {
	p, err := os.FindProcess(pid)
	if err != nil {
		return err
	}
	return p.Kill()
}

func startDetached() error {
	exe, err := os.Executable()
	if err != nil {
		return err
	}
	cmd := exec.Command(exe, "run")
	const createNoWindow, detachedProcess, newProcessGroup = 0x08000000, 0x00000008, 0x00000200
	cmd.SysProcAttr = &syscall.SysProcAttr{CreationFlags: createNoWindow | detachedProcess | newProcessGroup}
	if err := cmd.Start(); err != nil {
		return err
	}
	return cmd.Process.Release()
}

func removeHint(exe string) string {
	return fmt.Sprintf(`Remove-Item -Recurse "%s"`, filepath.Dir(exe))
}
