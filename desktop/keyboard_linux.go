package main

import (
	"errors"
	"fmt"
	"log"
	"os"
	"syscall"
)

// On Wayland, XTest only reaches X11 (XWayland) apps, so the kernel uinput
// device is preferred there. On X11, XTest types any character in any layout.
func preferUinput() bool { return os.Getenv("WAYLAND_DISPLAY") != "" }

func uinputWritable() bool { return syscall.Access("/dev/uinput", 2) == nil }

func newKeyboard() (keyboard, error) {
	if preferUinput() && uinputWritable() {
		if k, err := newUinputKeyboard(); err == nil {
			return k, nil
		} else {
			log.Printf("uinput unavailable (%v); falling back to X11", err)
		}
	}
	if os.Getenv("DISPLAY") != "" {
		if k, err := newX11Keyboard(); err == nil {
			return k, nil
		} else if !uinputWritable() {
			return nil, err
		}
	}
	if uinputWritable() {
		return newUinputKeyboard()
	}
	return nil, errors.New("no way to type: no X display and /dev/uinput is not writable; " + linuxSetupHint)
}

func typingBackendName() string {
	switch {
	case preferUinput() && uinputWritable():
		return "Linux uinput (Wayland)"
	case os.Getenv("DISPLAY") != "":
		return "X11 XTest"
	case uinputWritable():
		return "Linux uinput"
	}
	return "none available"
}

const linuxSetupHint = `allow access to the virtual keyboard device once with:
  echo 'KERNEL=="uinput", TAG+="uaccess"' | sudo tee /etc/udev/rules.d/60-voice-keyboard.rules
  echo uinput | sudo tee /etc/modules-load.d/voice-keyboard.conf
  sudo modprobe uinput && sudo udevadm control --reload && sudo udevadm trigger --name-match=uinput`

func checkTypingPermission(prompt bool) error {
	if preferUinput() && !uinputWritable() {
		return fmt.Errorf("on Wayland only X11 apps can be typed into until you %s", linuxSetupHint)
	}
	if os.Getenv("DISPLAY") == "" && os.Getenv("WAYLAND_DISPLAY") == "" && !uinputWritable() {
		return errors.New("no graphical session found; " + linuxSetupHint)
	}
	return nil
}
