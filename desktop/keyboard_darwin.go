package main

import (
	"errors"
	"fmt"
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
}

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
	return errors.New("allow voice-keyboard in System Settings → Privacy & Security → Accessibility; " +
		"typing starts as soon as it is allowed")
}

func (macKeyboard) Close() {}

func (macKeyboard) Type(text string) error {
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
		time.Sleep(2 * time.Millisecond)
	}
	return nil
}

func (macKeyboard) Key(name, state string) error {
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
