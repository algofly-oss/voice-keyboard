package main

import (
	"errors"

	"github.com/ebitengine/purego"
)

// Mouse events through CoreGraphics, with the same Accessibility permission
// as typing. Moves are relative to where the pointer is now.
type cgPoint struct{ X, Y float64 }

const (
	cgEventLeftMouseDown  = 1
	cgEventLeftMouseUp    = 2
	cgEventRightMouseDown = 3
	cgEventRightMouseUp   = 4
	cgEventMouseMoved     = 5
	cgEventLeftDragged    = 6
	cgEventRightDragged   = 7
	cgEventOtherDragged   = 27
	cgEventOtherMouseDown = 25
	cgEventOtherMouseUp   = 26
	cgScrollEventUnitLine = 1
)

var (
	cgEventCreate             func(source uintptr) uintptr
	cgEventGetLocation        func(event uintptr) cgPoint
	cgEventCreateMouseEvent   func(source uintptr, typ uint32, at cgPoint, button uint32) uintptr
	cgEventCreateScrollWheel2 func(source uintptr, units uint32, wheelCount uint32, wheel1, wheel2, wheel3 int32) uintptr
)

// macPointer remembers a held button: while it is down, motion must be
// posted as "dragged" events, or apps see a move without a drag.
type macPointer struct{ down string }

func newPointer() (pointer, error) {
	if err := load(); err != nil {
		return nil, err
	}
	cg, err := purego.Dlopen("/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics", purego.RTLD_NOW|purego.RTLD_GLOBAL)
	if err != nil {
		return nil, err
	}
	purego.RegisterLibFunc(&cgEventCreate, cg, "CGEventCreate")
	purego.RegisterLibFunc(&cgEventGetLocation, cg, "CGEventGetLocation")
	purego.RegisterLibFunc(&cgEventCreateMouseEvent, cg, "CGEventCreateMouseEvent")
	// The older CGEventCreateScrollWheelEvent is variadic; this one needs macOS 13.
	if _, err := purego.Dlsym(cg, "CGEventCreateScrollWheelEvent2"); err == nil {
		purego.RegisterLibFunc(&cgEventCreateScrollWheel2, cg, "CGEventCreateScrollWheelEvent2")
	}
	return &macPointer{}, nil
}

func location() cgPoint {
	e := cgEventCreate(0)
	defer cfRelease(e)
	return cgEventGetLocation(e)
}

func post(event uintptr) error {
	if event == 0 {
		return errors.New("could not create the mouse event")
	}
	cgEventPost(cgHIDEventTap, event)
	cfRelease(event)
	return nil
}

func (p *macPointer) Move(dx, dy int) error {
	if err := allowed(); err != nil {
		return err
	}
	at := location()
	at.X += float64(dx)
	at.Y += float64(dy)
	kind, button := uint32(cgEventMouseMoved), uint32(0)
	switch p.down {
	case "left":
		kind = cgEventLeftDragged
	case "right":
		kind, button = cgEventRightDragged, 1
	case "middle":
		kind, button = cgEventOtherDragged, 2
	}
	return post(cgEventCreateMouseEvent(0, kind, at, button))
}

func (p *macPointer) Click(button string) error {
	if err := allowed(); err != nil {
		return err
	}
	if err := p.Press(button); err != nil {
		return err
	}
	return p.Release(button)
}

// down, up, button number
var macButtons = map[string][3]uint32{
	"left":   {cgEventLeftMouseDown, cgEventLeftMouseUp, 0},
	"right":  {cgEventRightMouseDown, cgEventRightMouseUp, 1},
	"middle": {cgEventOtherMouseDown, cgEventOtherMouseUp, 2},
}

func (p *macPointer) Press(button string) error {
	if err := allowed(); err != nil {
		return err
	}
	b := macButtons[button]
	p.down = button
	return post(cgEventCreateMouseEvent(0, b[0], location(), b[2]))
}

func (p *macPointer) Release(button string) error {
	if err := allowed(); err != nil {
		return err
	}
	b := macButtons[button]
	if p.down == button {
		p.down = ""
	}
	return post(cgEventCreateMouseEvent(0, b[1], location(), b[2]))
}

func (p *macPointer) Scroll(dx, dy int) error {
	if err := allowed(); err != nil {
		return err
	}
	if cgEventCreateScrollWheel2 == nil {
		return errors.New("scrolling from the touchpad needs macOS 13 or later")
	}
	// wheel1 is vertical (positive scrolls up), wheel2 horizontal.
	return post(cgEventCreateScrollWheel2(0, cgScrollEventUnitLine, 2, int32(-dy), int32(-dx), 0))
}
