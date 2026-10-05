package main

import (
	"errors"
	"fmt"
	"os"
	"os/exec"
	"sync"
	"time"
	"unicode/utf16"
	"unsafe"

	"github.com/ebitengine/purego"
)

// CoreGraphics keyboard events carry a Unicode string, so any character types
// regardless of the keyboard layout. Posting them requires the Accessibility
// permission (System Settings → Privacy & Security → Accessibility).
var (
	loadOnce sync.Once
	loadErr  error

	cgEventCreateKeyboardEvent      func(source uintptr, keycode uint16, down bool) uintptr
	cgEventKeyboardSetUnicodeString func(event uintptr, length uint64, chars *uint16)
	cgEventPost                     func(tap uint32, event uintptr)
	cgEventSetFlags                 func(event uintptr, flags uint64)
	cfRelease                       func(ref uintptr)
	cfDictionaryCreate              func(allocator uintptr, keys, values *uintptr, count int64, keyCallbacks, valueCallbacks uintptr) uintptr
	axIsProcessTrusted              func() bool
	axIsProcessTrustedWithOptions   func(options uintptr) bool

	axTrustedCheckOptionPrompt uintptr // *CFStringRef
	cfBooleanTrue              uintptr // *CFBooleanRef
	cfKeyCallbacks             uintptr
	cfValueCallbacks           uintptr
)

const cgHIDEventTap = 0

var macKeys = map[string]uint16{
	"backspace": 0x33, "enter": 0x24, "left": 0x7B, "right": 0x7C, "down": 0x7D, "up": 0x7E,
	"escape": 0x35, "tab": 0x30, "space": 0x31,
	"f1": 0x7A, "f2": 0x78, "f3": 0x63, "f4": 0x76, "f5": 0x60, "f6": 0x61,
	"f7": 0x62, "f8": 0x64, "f9": 0x65, "f10": 0x6D, "f11": 0x67, "f12": 0x6F,
}

// ANSI key codes of letters and digits, for combinations.
var macChars = map[byte]uint16{
	'a': 0x00, 's': 0x01, 'd': 0x02, 'f': 0x03, 'h': 0x04, 'g': 0x05, 'z': 0x06, 'x': 0x07, 'c': 0x08, 'v': 0x09,
	'b': 0x0B, 'q': 0x0C, 'w': 0x0D, 'e': 0x0E, 'r': 0x0F, 'y': 0x10, 't': 0x11, 'o': 0x1F, 'u': 0x20, 'i': 0x22,
	'p': 0x23, 'l': 0x25, 'j': 0x26, 'k': 0x28, 'n': 0x2D, 'm': 0x2E,
	'1': 0x12, '2': 0x13, '3': 0x14, '4': 0x15, '5': 0x17, '6': 0x16, '7': 0x1A, '8': 0x1C, '9': 0x19, '0': 0x1D,
}

// Modifier flags of a combination. Control is Control, not Command: ctrl+c
// interrupts in Terminal (Command+C would copy).
const (
	cgEventFlagMaskShift     = 0x20000
	cgEventFlagMaskControl   = 0x40000
	cgEventFlagMaskAlternate = 0x80000
	cgEventFlagMaskCommand   = 0x100000
)

func load() error {
	loadOnce.Do(func() {
		const frameworks = "/System/Library/Frameworks/"
		cg, err := purego.Dlopen(frameworks+"CoreGraphics.framework/CoreGraphics", purego.RTLD_NOW|purego.RTLD_GLOBAL)
		if err != nil {
			loadErr = err
			return
		}
		cf, err := purego.Dlopen(frameworks+"CoreFoundation.framework/CoreFoundation", purego.RTLD_NOW|purego.RTLD_GLOBAL)
		if err != nil {
			loadErr = err
			return
		}
		ax, err := purego.Dlopen(frameworks+"ApplicationServices.framework/ApplicationServices", purego.RTLD_NOW|purego.RTLD_GLOBAL)
		if err != nil {
			loadErr = err
			return
		}
		purego.RegisterLibFunc(&cgEventCreateKeyboardEvent, cg, "CGEventCreateKeyboardEvent")
		purego.RegisterLibFunc(&cgEventKeyboardSetUnicodeString, cg, "CGEventKeyboardSetUnicodeString")
		purego.RegisterLibFunc(&cgEventPost, cg, "CGEventPost")
		purego.RegisterLibFunc(&cgEventSetFlags, cg, "CGEventSetFlags")
		purego.RegisterLibFunc(&cfRelease, cf, "CFRelease")
		purego.RegisterLibFunc(&cfDictionaryCreate, cf, "CFDictionaryCreate")
		purego.RegisterLibFunc(&axIsProcessTrusted, ax, "AXIsProcessTrusted")
		purego.RegisterLibFunc(&axIsProcessTrustedWithOptions, ax, "AXIsProcessTrustedWithOptions")
		for _, sym := range []struct {
			lib  uintptr
			name string
			dst  *uintptr
		}{
			{ax, "kAXTrustedCheckOptionPrompt", &axTrustedCheckOptionPrompt},
			{cf, "kCFBooleanTrue", &cfBooleanTrue},
			{cf, "kCFTypeDictionaryKeyCallBacks", &cfKeyCallbacks},
			{cf, "kCFTypeDictionaryValueCallBacks", &cfValueCallbacks},
		} {
			if *sym.dst, loadErr = purego.Dlsym(sym.lib, sym.name); loadErr != nil {
				return
			}
		}
	})
	return loadErr
}

type macKeyboard struct{}

func newKeyboard() (keyboard, error) {
	if err := load(); err != nil {
		return nil, fmt.Errorf("loading CoreGraphics: %w", err)
	}
	return macKeyboard{}, nil
}

func typingBackendName() string { return "macOS CoreGraphics events" }

// checkTypingPermission reports a missing Accessibility grant. With prompt,
// macOS shows its "allow this app to control your computer" dialog and the
// settings pane is opened.
func checkTypingPermission(prompt bool) error {
	if err := load(); err != nil {
		return err
	}
	if axIsProcessTrusted() {
		return nil
	}
	if prompt {
		// Dlsym returns the addresses of these CF globals inside system libraries,
		// not Go memory, so reading through them is safe (vet cannot tell).
		key := *(*uintptr)(unsafe.Pointer(axTrustedCheckOptionPrompt))
		value := *(*uintptr)(unsafe.Pointer(cfBooleanTrue))
		options := cfDictionaryCreate(0, &key, &value, 1, cfKeyCallbacks, cfValueCallbacks)
		axIsProcessTrustedWithOptions(options)
		cfRelease(options)
		_ = exec.Command("open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility").Start()
	}
	return errors.New("allow vkeyboard in System Settings → Privacy & Security → Accessibility; " +
		"typing starts as soon as it is allowed")
}

func (macKeyboard) Close() {}

// Without Accessibility, macOS drops posted events without an error; checking
// first makes every blocked keystroke, key and pointer action fail visibly.
var errAccessibility = errors.New("macOS blocks it: vkeyboard is not allowed under Privacy & Security → Accessibility; run: vkeyboard permission")

func allowed() error {
	if !axIsProcessTrusted() {
		return errAccessibility
	}
	return nil
}

func (macKeyboard) Type(text string) error {
	if err := allowed(); err != nil {
		return err
	}
	for _, r := range text {
		if r == '\n' {
			if err := (macKeyboard{}).Key("enter", "press"); err != nil {
				return err
			}
			continue
		}
		units := utf16.Encode([]rune{r})
		for _, down := range []bool{true, false} {
			event := cgEventCreateKeyboardEvent(0, 0, down)
			if event == 0 {
				return errors.New("CGEventCreateKeyboardEvent failed")
			}
			cgEventKeyboardSetUnicodeString(event, uint64(len(units)), &units[0])
			cgEventPost(cgHIDEventTap, event)
			cfRelease(event)
		}
		// Some apps (Electron, terminals) drop characters posted back to back.
		pause(2 * time.Millisecond)
	}
	return nil
}

func (macKeyboard) Key(name, state string) error {
	if err := allowed(); err != nil {
		return err
	}
	if c, ok := parseCombo(name); ok {
		if state == "up" {
			return nil
		}
		code, ok := macKeys[c.key]
		if !ok {
			code = macChars[c.key[0]]
		}
		var flags uint64
		for _, m := range []struct {
			on   bool
			flag uint64
		}{{c.ctrl, cgEventFlagMaskControl}, {c.alt, cgEventFlagMaskAlternate}, {c.shift, cgEventFlagMaskShift}, {c.meta, cgEventFlagMaskCommand}} {
			if m.on {
				flags |= m.flag
			}
		}
		for _, down := range []bool{true, false} {
			event := cgEventCreateKeyboardEvent(0, code, down)
			if event == 0 {
				return errors.New("CGEventCreateKeyboardEvent failed")
			}
			cgEventSetFlags(event, flags)
			cgEventPost(cgHIDEventTap, event)
			cfRelease(event)
		}
		return nil
	}
	code, ok := macKeys[name]
	if !ok {
		return fmt.Errorf("unsupported key %q", name)
	}
	return pressKey(func(down bool) error {
		event := cgEventCreateKeyboardEvent(0, code, down)
		if event == 0 {
			return errors.New("CGEventCreateKeyboardEvent failed")
		}
		cgEventPost(cgHIDEventTap, event)
		cfRelease(event)
		return nil
	}, state)
}

// permissionSteps: what to do if no prompt appears. A stale entry for an
// earlier build (same name, different binary) blocks the prompt.
func permissionSteps() string {
	exe, _ := os.Executable()
	return `If no prompt appears, or allowing it does not help:
  1. System Settings → Privacy & Security → Accessibility (it has been opened for you)
  2. select vkeyboard and remove it with −  (an entry for an older build)
  3. click +, press ⌘⇧G, enter ` + exe + `, and turn it on`
}
