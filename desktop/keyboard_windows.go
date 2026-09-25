package main

import (
	"fmt"
	"syscall"
	"unicode/utf16"
	"unsafe"
)

// SendInput with KEYEVENTF_UNICODE types any character regardless of the
// active keyboard layout.
var (
	user32    = syscall.NewLazyDLL("user32.dll")
	sendInput = user32.NewProc("SendInput")
)

const (
	inputKeyboard    = 1
	keyEventExtended = 0x0001
	keyEventKeyUp    = 0x0002
	keyEventUnicode  = 0x0004
)

// input mirrors the Win32 INPUT struct holding a KEYBDINPUT. The union is
// sized by MOUSEINPUT (32 bytes on 64-bit), hence the trailing padding.
type input struct {
	typ       uint32
	_         uint32
	vk        uint16
	scan      uint16
	flags     uint32
	time      uint32
	extraInfo uintptr
	_         [8]byte
}

var virtualKeys = map[string]struct {
	vk       uint16
	extended bool
}{
	"backspace": {0x08, false}, "enter": {0x0D, false},
	"left": {0x25, true}, "up": {0x26, true}, "right": {0x27, true}, "down": {0x28, true},
}

type windowsKeyboard struct{}

func newKeyboard() (keyboard, error) {
	if unsafe.Sizeof(input{}) != 40 {
		return nil, fmt.Errorf("unexpected INPUT size %d", unsafe.Sizeof(input{}))
	}
	return windowsKeyboard{}, nil
}

func typingBackendName() string               { return "Windows SendInput" }
func checkTypingPermission(prompt bool) error { return nil }
func (windowsKeyboard) Close()                {}

func (windowsKeyboard) Type(text string) error {
	var events []input
	for _, r := range text {
		if r == '\n' {
			events = append(events, input{typ: inputKeyboard, vk: 0x0D}, input{typ: inputKeyboard, vk: 0x0D, flags: keyEventKeyUp})
			continue
		}
		if r == '\r' {
			continue
		}
		for _, unit := range utf16.Encode([]rune{r}) {
			events = append(events,
				input{typ: inputKeyboard, scan: unit, flags: keyEventUnicode},
				input{typ: inputKeyboard, scan: unit, flags: keyEventUnicode | keyEventKeyUp})
		}
	}
	if configuredDelay <= 0 {
		return send(events) // the whole piece in one call: as fast as Windows allows
	}
	for i := 0; i < len(events); i += 2 { // key down + up per character, then pause
		if err := send(events[i:min(i+2, len(events))]); err != nil {
			return err
		}
		pause(0)
	}
	return nil
}

func (windowsKeyboard) Key(name, state string) error {
	k, ok := virtualKeys[name]
	if !ok {
		return fmt.Errorf("unsupported key %q", name)
	}
	return pressKey(func(down bool) error {
		flags := uint32(0)
		if k.extended {
			flags |= keyEventExtended
		}
		if !down {
			flags |= keyEventKeyUp
		}
		return send([]input{{typ: inputKeyboard, vk: k.vk, flags: flags}})
	}, state)
}

func send(events []input) error {
	if len(events) == 0 {
		return nil
	}
	n, _, err := sendInput.Call(uintptr(len(events)), uintptr(unsafe.Pointer(&events[0])), unsafe.Sizeof(events[0]))
	if int(n) != len(events) {
		// Blocked by UIPI when the focused window runs as administrator.
		return fmt.Errorf("SendInput sent %d of %d events: %v", n, len(events), err)
	}
	return nil
}
