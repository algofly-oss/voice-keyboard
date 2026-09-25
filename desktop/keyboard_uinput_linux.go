package main

import (
	"encoding/binary"
	"fmt"
	"os"
	"syscall"
	"time"
	"unsafe"
)

// uinputKeyboard creates a virtual kernel keyboard. It works everywhere,
// including Wayland where XTest cannot reach native apps, but it sends key
// codes rather than characters: text is mapped through a US layout, and
// characters outside ASCII cannot be typed. Needs write access to /dev/uinput
// (see linuxSetupHint).
type uinputKeyboard struct{ f *os.File }

const (
	evSyn        = 0x00
	evKey        = 0x01
	synReport    = 0
	uiSetEvBit   = 0x40045564 // _IOW('U', 100, int)
	uiSetKeyBit  = 0x40045565 // _IOW('U', 101, int)
	uiDevSetup   = 0x405c5503 // _IOW('U', 3, struct uinput_setup)
	uiDevCreate  = 0x5501     // _IO('U', 1)
	uiDevDestroy = 0x5502     // _IO('U', 2)
	keyLeftShift = 42
)

var uinputKeys = map[string]uint16{
	"backspace": 14, "enter": 28, "up": 103, "left": 105, "right": 106, "down": 108,
}

// usLayout maps printable ASCII to (Linux key code, needs shift).
var usLayout = func() map[rune][2]uint16 {
	m := map[rune][2]uint16{' ': {57, 0}, '\n': {28, 0}, '\t': {15, 0}}
	rows := []struct {
		plain, shifted string
		first          uint16
	}{
		{"1234567890-=", "!@#$%^&*()_+", 2},
		{"qwertyuiop[]", "QWERTYUIOP{}", 16},
		{"asdfghjkl;'`", "ASDFGHJKL:\"~", 30},
		{"\\zxcvbnm,./", "|ZXCVBNM<>?", 43},
	}
	for _, row := range rows {
		shifted := []rune(row.shifted)
		for i, r := range row.plain {
			m[r] = [2]uint16{row.first + uint16(i), 0}
			m[shifted[i]] = [2]uint16{row.first + uint16(i), 1}
		}
	}
	return m
}()

func newUinputKeyboard() (*uinputKeyboard, error) {
	f, err := os.OpenFile("/dev/uinput", os.O_WRONLY|syscall.O_NONBLOCK, 0)
	if err != nil {
		return nil, err
	}
	k := &uinputKeyboard{f}
	if err := k.ioctl(uiSetEvBit, evKey); err != nil {
		f.Close()
		return nil, err
	}
	for code := uintptr(1); code < 256; code++ {
		if err := k.ioctl(uiSetKeyBit, code); err != nil {
			f.Close()
			return nil, err
		}
	}
	// struct uinput_setup { struct input_id id; char name[80]; __u32 ff_effects_max; }
	var setup [92]byte
	binary.LittleEndian.PutUint16(setup[0:], 0x06) // BUS_VIRTUAL
	binary.LittleEndian.PutUint16(setup[2:], 0x1209)
	binary.LittleEndian.PutUint16(setup[4:], 0x5657)
	copy(setup[8:], "Voice Keyboard")
	if err := k.ioctl(uiDevSetup, uintptr(unsafe.Pointer(&setup[0]))); err != nil {
		f.Close()
		return nil, err
	}
	if err := k.ioctl(uiDevCreate, 0); err != nil {
		f.Close()
		return nil, err
	}
	time.Sleep(300 * time.Millisecond) // give the compositor time to pick up the new device
	return k, nil
}

func (k *uinputKeyboard) ioctl(request, arg uintptr) error {
	if _, _, errno := syscall.Syscall(syscall.SYS_IOCTL, k.f.Fd(), request, arg); errno != 0 {
		return errno
	}
	return nil
}

// emit writes one struct input_event (timeval, type, code, value): 24 bytes on 64-bit.
func (k *uinputKeyboard) emit(typ, code uint16, value int32) error {
	var ev [24]byte
	binary.LittleEndian.PutUint16(ev[16:], typ)
	binary.LittleEndian.PutUint16(ev[18:], code)
	binary.LittleEndian.PutUint32(ev[20:], uint32(value))
	_, err := k.f.Write(ev[:])
	return err
}

func (k *uinputKeyboard) key(code uint16, down bool) error {
	value := int32(0)
	if down {
		value = 1
	}
	if err := k.emit(evKey, code, value); err != nil {
		return err
	}
	return k.emit(evSyn, synReport, 0)
}

func (k *uinputKeyboard) Type(text string) error {
	var skipped int
	for _, r := range text {
		pos, ok := usLayout[r]
		if !ok {
			skipped++
			continue
		}
		if pos[1] == 1 {
			k.key(keyLeftShift, true)
		}
		k.key(pos[0], true)
		k.key(pos[0], false)
		if pos[1] == 1 {
			k.key(keyLeftShift, false)
		}
		time.Sleep(2 * time.Millisecond)
	}
	if skipped > 0 {
		return fmt.Errorf("skipped %d non-ASCII character(s); uinput can only type US-layout keys", skipped)
	}
	return nil
}

func (k *uinputKeyboard) Key(name, state string) error {
	code, ok := uinputKeys[name]
	if !ok {
		return fmt.Errorf("unsupported key %q", name)
	}
	return pressKey(func(down bool) error { return k.key(code, down) }, state)
}

func (k *uinputKeyboard) Close() {
	k.ioctl(uiDevDestroy, 0)
	k.f.Close()
}
