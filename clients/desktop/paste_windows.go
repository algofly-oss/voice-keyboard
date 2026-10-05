package main

import (
	"errors"
	"syscall"
	"time"
	"unicode/utf16"
	"unsafe"
)

var (
	kernel32dll             = syscall.NewLazyDLL("kernel32.dll")
	openClipboard           = user32.NewProc("OpenClipboard")
	closeClipboard          = user32.NewProc("CloseClipboard")
	emptyClipboard          = user32.NewProc("EmptyClipboard")
	enumClipboardFormats    = user32.NewProc("EnumClipboardFormats")
	getClipboardData        = user32.NewProc("GetClipboardData")
	setClipboardData        = user32.NewProc("SetClipboardData")
	registerClipboardFormat = user32.NewProc("RegisterClipboardFormatW")
	globalAlloc             = kernel32dll.NewProc("GlobalAlloc")
	globalLock              = kernel32dll.NewProc("GlobalLock")
	globalUnlock            = kernel32dll.NewProc("GlobalUnlock")
)

const (
	cfText, cfOEMText, cfUnicodeText, cfLocale = 1, 7, 13, 16
	gmemMoveable                               = 2
)

func pasteText(_ keyboard, text string) error {
	var saved string
	var hadText bool
	err := withClipboard(func() (err error) {
		if saved, hadText, err = readClipboardText(); err != nil {
			return err
		}
		return writeClipboardText(text)
	})
	if err != nil {
		return err
	}
	const vkControl, vkV = 0x11, 0x56
	if err := send([]input{{typ: inputKeyboard, vk: vkControl}, {typ: inputKeyboard, vk: vkV},
		{typ: inputKeyboard, vk: vkV, flags: keyEventKeyUp}, {typ: inputKeyboard, vk: vkControl, flags: keyEventKeyUp}}); err != nil {
		return err
	}
	time.Sleep(250 * time.Millisecond) // let the app read the clipboard before it is restored
	return withClipboard(func() error {
		if !hadText {
			emptyClipboard.Call()
			return nil
		}
		return writeClipboardText(saved)
	})
}

func withClipboard(f func() error) error {
	for attempt := 0; ; attempt++ {
		if r, _, _ := openClipboard.Call(0); r != 0 {
			break
		}
		if attempt == 20 {
			return errors.New("the clipboard is busy")
		}
		time.Sleep(10 * time.Millisecond)
	}
	defer closeClipboard.Call()
	return f()
}

// readClipboardText refuses anything but plain text, so pasting never loses
// an image, files or formatting the user copied.
func readClipboardText() (string, bool, error) {
	for format := uintptr(0); ; {
		format, _, _ = enumClipboardFormats.Call(format)
		if format == 0 {
			break
		}
		if format != cfText && format != cfOEMText && format != cfUnicodeText && format != cfLocale {
			return "", false, errors.New("the clipboard holds more than plain text")
		}
	}
	h, _, _ := getClipboardData.Call(cfUnicodeText)
	if h == 0 {
		return "", false, nil
	}
	p, _, _ := globalLock.Call(h)
	if p == 0 {
		return "", false, nil
	}
	defer globalUnlock.Call(h)
	var units []uint16
	for i := uintptr(0); ; i += 2 {
		u := *(*uint16)(unsafe.Pointer(p + i))
		if u == 0 {
			break
		}
		units = append(units, u)
	}
	return string(utf16.Decode(units)), true, nil
}

// writeClipboardText also marks the content so Windows clipboard history and
// clipboard managers ignore it: neither the dictated text nor the restored
// copy shows up there.
func writeClipboardText(s string) error {
	emptyClipboard.Call()
	units := utf16.Encode([]rune(s + "\x00"))
	h, _, _ := globalAlloc.Call(gmemMoveable, uintptr(len(units)*2))
	if h == 0 {
		return errors.New("GlobalAlloc failed")
	}
	p, _, _ := globalLock.Call(h)
	copy(unsafe.Slice((*uint16)(unsafe.Pointer(p)), len(units)), units)
	globalUnlock.Call(h)
	if r, _, _ := setClipboardData.Call(cfUnicodeText, h); r == 0 {
		return errors.New("SetClipboardData failed")
	}
	name, _ := syscall.UTF16PtrFromString("ExcludeClipboardContentFromMonitorProcessing")
	if format, _, _ := registerClipboardFormat.Call(uintptr(unsafe.Pointer(name))); format != 0 {
		marker, _, _ := globalAlloc.Call(gmemMoveable, 1)
		setClipboardData.Call(format, marker)
	}
	return nil
}
