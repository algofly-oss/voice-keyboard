package main

import (
	"fmt"
	"unsafe"
)

const (
	inputMouse      = 0
	mouseMove       = 0x0001
	mouseLeftDown   = 0x0002
	mouseLeftUp     = 0x0004
	mouseRightDown  = 0x0008
	mouseRightUp    = 0x0010
	mouseMiddleDown = 0x0020
	mouseMiddleUp   = 0x0040
	mouseWheel      = 0x0800
	mouseHWheel     = 0x1000
	wheelDelta      = 120
)

// mouseInput mirrors the Win32 INPUT struct holding a MOUSEINPUT (40 bytes on 64-bit).
type mouseInput struct {
	typ       uint32
	_         uint32
	dx, dy    int32
	mouseData uint32
	flags     uint32
	time      uint32
	_         uint32
	extraInfo uintptr
}

type windowsPointer struct{}

func newPointer() (pointer, error) {
	if unsafe.Sizeof(mouseInput{}) != 40 {
		return nil, fmt.Errorf("unexpected INPUT size %d", unsafe.Sizeof(mouseInput{}))
	}
	return windowsPointer{}, nil
}

func sendMouse(events ...mouseInput) error {
	n, _, err := sendInput.Call(uintptr(len(events)), uintptr(unsafe.Pointer(&events[0])), unsafe.Sizeof(events[0]))
	if int(n) != len(events) {
		return fmt.Errorf("SendInput sent %d of %d events: %v", n, len(events), err)
	}
	return nil
}

func (windowsPointer) Move(dx, dy int) error {
	return sendMouse(mouseInput{typ: inputMouse, dx: int32(dx), dy: int32(dy), flags: mouseMove})
}

func (windowsPointer) Click(button string) error {
	flags := map[string][2]uint32{
		"left": {mouseLeftDown, mouseLeftUp}, "right": {mouseRightDown, mouseRightUp}, "middle": {mouseMiddleDown, mouseMiddleUp},
	}[button]
	return sendMouse(mouseInput{typ: inputMouse, flags: flags[0]}, mouseInput{typ: inputMouse, flags: flags[1]})
}

func (windowsPointer) Scroll(dx, dy int) error {
	var events []mouseInput
	if dy != 0 { // positive wheel data scrolls up
		events = append(events, mouseInput{typ: inputMouse, mouseData: uint32(int32(-dy * wheelDelta)), flags: mouseWheel})
	}
	if dx != 0 {
		events = append(events, mouseInput{typ: inputMouse, mouseData: uint32(int32(dx * wheelDelta)), flags: mouseHWheel})
	}
	if len(events) == 0 {
		return nil
	}
	return sendMouse(events...)
}
