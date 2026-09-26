package main

import (
	"encoding/binary"
	"errors"
	"fmt"
	"os"
	"syscall"
	"time"
	"unsafe"

	"github.com/ebitengine/purego"
)

// Same choice as typing: uinput on Wayland, XTest on X11.
func newPointer() (pointer, error) {
	if preferUinput() && uinputWritable() {
		return newUinputPointer()
	}
	if os.Getenv("DISPLAY") != "" {
		if p, err := newX11Pointer(); err == nil {
			return p, nil
		} else if !uinputWritable() {
			return nil, err
		}
	}
	if uinputWritable() {
		return newUinputPointer()
	}
	return nil, errors.New("no way to move the pointer: no X display and /dev/uinput is not writable")
}

// ---- X11 (XTest) ----

type x11Pointer struct {
	display             uintptr
	xFlush              func(display uintptr) int32
	xTestRelativeMotion func(display uintptr, dx, dy int32, delay uint64) int32
	xTestButton         func(display uintptr, button uint32, press int32, delay uint64) int32
}

func newX11Pointer() (*x11Pointer, error) {
	x11, err := purego.Dlopen("libX11.so.6", purego.RTLD_NOW|purego.RTLD_GLOBAL)
	if err != nil {
		return nil, fmt.Errorf("libX11 not found: %w", err)
	}
	xtst, err := purego.Dlopen("libXtst.so.6", purego.RTLD_NOW|purego.RTLD_GLOBAL)
	if err != nil {
		return nil, fmt.Errorf("libXtst not found: %w", err)
	}
	var open func(name *byte) uintptr
	purego.RegisterLibFunc(&open, x11, "XOpenDisplay")
	p := &x11Pointer{}
	purego.RegisterLibFunc(&p.xFlush, x11, "XFlush")
	purego.RegisterLibFunc(&p.xTestRelativeMotion, xtst, "XTestFakeRelativeMotionEvent")
	purego.RegisterLibFunc(&p.xTestButton, xtst, "XTestFakeButtonEvent")
	if p.display = open(nil); p.display == 0 {
		return nil, errors.New("cannot open the X display")
	}
	return p, nil
}

func (p *x11Pointer) Move(dx, dy int) error {
	p.xTestRelativeMotion(p.display, int32(dx), int32(dy), 0)
	p.xFlush(p.display)
	return nil
}

func (p *x11Pointer) press(button uint32, times int) {
	for i := 0; i < times; i++ {
		p.xTestButton(p.display, button, 1, 0)
		p.xTestButton(p.display, button, 0, 0)
	}
	p.xFlush(p.display)
}

var x11Buttons = map[string]uint32{"left": 1, "middle": 2, "right": 3}

func (p *x11Pointer) Click(button string) error {
	p.press(x11Buttons[button], 1)
	return nil
}

func (p *x11Pointer) Press(button string) error {
	p.xTestButton(p.display, x11Buttons[button], 1, 0)
	p.xFlush(p.display)
	return nil
}

func (p *x11Pointer) Release(button string) error {
	p.xTestButton(p.display, x11Buttons[button], 0, 0)
	p.xFlush(p.display)
	return nil
}

// X11 scrolls with buttons: 4 up, 5 down, 6 left, 7 right, one click per step.
func (p *x11Pointer) Scroll(dx, dy int) error {
	if dy < 0 {
		p.press(4, -dy)
	} else if dy > 0 {
		p.press(5, dy)
	}
	if dx < 0 {
		p.press(6, -dx)
	} else if dx > 0 {
		p.press(7, dx)
	}
	return nil
}

// ---- uinput: a virtual mouse of its own ----

const (
	evRel       = 0x02
	relX        = 0x00
	relY        = 0x01
	relHWheel   = 0x06
	relWheel    = 0x08
	btnLeft     = 0x110
	btnRight    = 0x111
	btnMiddle   = 0x112
	uiSetRelBit = 0x40045566 // _IOW('U', 102, int)
)

type uinputPointer struct{ dev *uinputKeyboard } // same file and event helpers

func newUinputPointer() (*uinputPointer, error) {
	f, err := os.OpenFile("/dev/uinput", os.O_WRONLY|syscall.O_NONBLOCK, 0)
	if err != nil {
		return nil, err
	}
	d := &uinputKeyboard{f}
	setup := []struct{ request, arg uintptr }{
		{uiSetEvBit, evKey}, {uiSetEvBit, evRel},
		{uiSetKeyBit, btnLeft}, {uiSetKeyBit, btnRight}, {uiSetKeyBit, btnMiddle},
		{uiSetRelBit, relX}, {uiSetRelBit, relY}, {uiSetRelBit, relWheel}, {uiSetRelBit, relHWheel},
	}
	for _, s := range setup {
		if err := d.ioctl(s.request, s.arg); err != nil {
			f.Close()
			return nil, err
		}
	}
	var dev [92]byte // struct uinput_setup, as for the keyboard
	binary.LittleEndian.PutUint16(dev[0:], 0x06)
	binary.LittleEndian.PutUint16(dev[2:], 0x1209)
	binary.LittleEndian.PutUint16(dev[4:], 0x5658)
	copy(dev[8:], "Voice Keyboard Mouse")
	if err := d.ioctl(uiDevSetup, uintptr(unsafe.Pointer(&dev[0]))); err != nil {
		f.Close()
		return nil, err
	}
	if err := d.ioctl(uiDevCreate, 0); err != nil {
		f.Close()
		return nil, err
	}
	time.Sleep(300 * time.Millisecond) // let the compositor pick up the new device
	return &uinputPointer{d}, nil
}

func (p *uinputPointer) rel(pairs ...[2]int) error {
	for _, v := range pairs {
		if v[1] == 0 {
			continue
		}
		if err := p.dev.emit(evRel, uint16(v[0]), int32(v[1])); err != nil {
			return err
		}
	}
	return p.dev.emit(evSyn, synReport, 0)
}

func (p *uinputPointer) Move(dx, dy int) error { return p.rel([2]int{relX, dx}, [2]int{relY, dy}) }

var uinputButtons = map[string]uint16{"left": btnLeft, "right": btnRight, "middle": btnMiddle}

func (p *uinputPointer) Click(button string) error {
	if err := p.Press(button); err != nil {
		return err
	}
	return p.Release(button)
}

func (p *uinputPointer) Press(button string) error   { return p.dev.key(uinputButtons[button], true) }
func (p *uinputPointer) Release(button string) error { return p.dev.key(uinputButtons[button], false) }

// The wheel axis counts up as positive.
func (p *uinputPointer) Scroll(dx, dy int) error {
	return p.rel([2]int{relWheel, -dy}, [2]int{relHWheel, dx})
}
