package main

import (
	"errors"
	"fmt"
	"time"
	"unsafe"

	"github.com/ebitengine/purego"
)

// x11Keyboard types through the XTest extension. libX11 and libXtst are part
// of every X11 desktop and are loaded at run time, so the binary has no build
// or install dependency on them. Characters missing from the current layout
// are typed by briefly binding them to an unused keycode (as xdotool does).
type x11Keyboard struct {
	display uintptr

	xFlush                 func(display uintptr) int32
	xSync                  func(display uintptr, discard int32) int32
	xCloseDisplay          func(display uintptr) int32
	xDisplayKeycodes       func(display uintptr, min, max *int32) int32
	xGetKeyboardMapping    func(display uintptr, first uint8, count int32, perKeycode *int32) unsafe.Pointer
	xChangeKeyboardMapping func(display uintptr, first, perKeycode int32, keysyms *uint64, count int32) int32
	xFree                  func(ptr unsafe.Pointer) int32
	xTestFakeKeyEvent      func(display uintptr, keycode uint32, press int32, delay uint64) int32

	minCode, maxCode int32
	shift            uint32
	scratch          uint32 // a keycode with no symbols, used for unmapped characters
}

const (
	xkShiftL    = 0xffe1
	xkBackSpace = 0xff08
	xkReturn    = 0xff0d
	xkTab       = 0xff09
)

var x11Keys = map[string]uint64{
	"backspace": xkBackSpace, "enter": xkReturn,
	"left": 0xff51, "up": 0xff52, "right": 0xff53, "down": 0xff54,
}

func newX11Keyboard() (*x11Keyboard, error) {
	x11, err := purego.Dlopen("libX11.so.6", purego.RTLD_NOW|purego.RTLD_GLOBAL)
	if err != nil {
		return nil, fmt.Errorf("libX11 not found: %w", err)
	}
	xtst, err := purego.Dlopen("libXtst.so.6", purego.RTLD_NOW|purego.RTLD_GLOBAL)
	if err != nil {
		return nil, fmt.Errorf("libXtst not found (install libxtst6): %w", err)
	}
	k := &x11Keyboard{}
	var xOpenDisplay func(name uintptr) uintptr
	purego.RegisterLibFunc(&xOpenDisplay, x11, "XOpenDisplay")
	purego.RegisterLibFunc(&k.xFlush, x11, "XFlush")
	purego.RegisterLibFunc(&k.xSync, x11, "XSync")
	purego.RegisterLibFunc(&k.xCloseDisplay, x11, "XCloseDisplay")
	purego.RegisterLibFunc(&k.xDisplayKeycodes, x11, "XDisplayKeycodes")
	purego.RegisterLibFunc(&k.xGetKeyboardMapping, x11, "XGetKeyboardMapping")
	purego.RegisterLibFunc(&k.xChangeKeyboardMapping, x11, "XChangeKeyboardMapping")
	purego.RegisterLibFunc(&k.xFree, x11, "XFree")
	purego.RegisterLibFunc(&k.xTestFakeKeyEvent, xtst, "XTestFakeKeyEvent")

	if k.display = xOpenDisplay(0); k.display == 0 {
		return nil, errors.New("cannot open the X display (is DISPLAY set?)")
	}
	k.xDisplayKeycodes(k.display, &k.minCode, &k.maxCode)
	positions, err := k.keymap()
	shift, ok := positions[xkShiftL]
	if err != nil || !ok {
		k.Close()
		return nil, errors.New("no Shift key in the X keyboard mapping")
	}
	k.shift = shift.code
	return k, nil
}

func (k *x11Keyboard) Close() {
	if k.display != 0 {
		k.xCloseDisplay(k.display)
		k.display = 0
	}
}

type keyPosition struct {
	code  uint32
	shift bool
}

// keymap reads the current layout: where each keysym sits (plain or shifted),
// plus an empty keycode to borrow for characters the layout lacks.
func (k *x11Keyboard) keymap() (map[uint64]keyPosition, error) {
	count := k.maxCode - k.minCode + 1
	var per int32
	mapping := k.xGetKeyboardMapping(k.display, uint8(k.minCode), count, &per)
	if mapping == nil {
		return nil, errors.New("XGetKeyboardMapping failed")
	}
	defer k.xFree(mapping)
	syms := unsafe.Slice((*uint64)(mapping), int(count*per))
	positions := map[uint64]keyPosition{}
	k.scratch = 0
	for i := int32(0); i < count; i++ {
		row := syms[i*per : (i+1)*per]
		empty := true
		for level, sym := range row {
			if sym != 0 {
				empty = false
			}
			if _, seen := positions[sym]; sym != 0 && level < 2 && !seen {
				positions[sym] = keyPosition{uint32(k.minCode + i), level == 1}
			}
		}
		if empty && k.scratch == 0 {
			k.scratch = uint32(k.minCode + i)
		}
	}
	return positions, nil
}

func (k *x11Keyboard) tap(code uint32, shift bool) {
	if shift {
		k.xTestFakeKeyEvent(k.display, k.shift, 1, 0)
	}
	k.xTestFakeKeyEvent(k.display, code, 1, 0)
	k.xTestFakeKeyEvent(k.display, code, 0, 0)
	if shift {
		k.xTestFakeKeyEvent(k.display, k.shift, 0, 0)
	}
}

func keysymFor(r rune) uint64 {
	switch {
	case r == '\n':
		return xkReturn
	case r == '\t':
		return xkTab
	case r >= 0x20 && r <= 0x7e, r >= 0xa0 && r <= 0xff:
		return uint64(r) // Latin-1 keysyms equal their code points
	default:
		return 0x01000000 | uint64(r) // Unicode keysym
	}
}

func (k *x11Keyboard) Type(text string) error {
	positions, err := k.keymap()
	if err != nil {
		return err
	}
	for _, r := range text {
		if r == '\r' {
			continue
		}
		sym := keysymFor(r)
		if p, ok := positions[sym]; ok {
			k.tap(p.code, p.shift)
			k.xFlush(k.display)
			pause(0)
			continue
		}
		if k.scratch == 0 {
			return fmt.Errorf("no free keycode to type %q", r)
		}
		// Bind the character to the spare keycode, type it, then unbind it.
		syms := []uint64{sym, sym}
		k.xChangeKeyboardMapping(k.display, int32(k.scratch), 2, &syms[0], 1)
		k.xSync(k.display, 0)
		time.Sleep(10 * time.Millisecond) // let clients process MappingNotify
		k.tap(k.scratch, false)
		k.xSync(k.display, 0)
		time.Sleep(10 * time.Millisecond)
		none := []uint64{0, 0}
		k.xChangeKeyboardMapping(k.display, int32(k.scratch), 2, &none[0], 1)
		k.xSync(k.display, 0)
	}
	return nil
}

func (k *x11Keyboard) Key(name, state string) error {
	sym, ok := x11Keys[name]
	if !ok {
		return fmt.Errorf("unsupported key %q", name)
	}
	positions, err := k.keymap()
	p, found := positions[sym]
	if err != nil || !found {
		return fmt.Errorf("key %q is not in the X keyboard mapping", name)
	}
	return pressKey(func(down bool) error {
		k.xTestFakeKeyEvent(k.display, p.code, map[bool]int32{true: 1, false: 0}[down], 0)
		k.xFlush(k.display)
		return nil
	}, state)
}
