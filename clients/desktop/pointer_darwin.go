package main

import (
	"errors"
	"math"
	"time"

	"github.com/ebitengine/purego"
)

// Mouse events through CoreGraphics, with the same Accessibility permission
// as typing. Moves are relative to where the pointer is now.
type cgPoint struct{ X, Y float64 }

type cgRect struct {
	Origin cgPoint
	Size   struct{ W, H float64 }
}

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

	// CGEventField values
	cgMouseEventClickState = 1 // 2 on the second click of a double click
	cgMouseEventDeltaX     = 4 // the motion itself: the Dock and hot corners
	cgMouseEventDeltaY     = 5 // react to pushing against a screen edge
)

// A second click within this time and distance is a double click (the macOS default is 0.5 s).
const (
	doubleClickTime = 500 * time.Millisecond
	doubleClickPx   = 6
)

var (
	cgEventCreate             func(source uintptr) uintptr
	cgEventGetLocation        func(event uintptr) cgPoint
	cgEventCreateMouseEvent   func(source uintptr, typ uint32, at cgPoint, button uint32) uintptr
	cgEventCreateScrollWheel2 func(source uintptr, units uint32, wheelCount uint32, wheel1, wheel2, wheel3 int32) uintptr
	cgEventSetIntegerField    func(event uintptr, field uint32, value int64)
	cgEventSourceCreate       func(state int32) uintptr
	cgGetDisplaysWithPoint    func(at cgPoint, max uint32, displays *uint32, count *uint32) int32
	cgDisplayBounds           func(display uint32) cgRect

	// Events from the HID system's source, like a real mouse's: the Dock and
	// hot corners follow the hardware input state.
	hidSource uintptr
)

const cgEventSourceStateHIDSystemState = 1

// macPointer remembers a held button: while it is down, motion must be
// posted as "dragged" events, or apps see a move without a drag.
type macPointer struct {
	down string
	// Consecutive clicks: macOS counts a double click only from the click
	// state in the events, which synthetic events leave at 1.
	lastButton string
	lastAt     time.Time
	lastPoint  cgPoint
	clicks     int64
}

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
	purego.RegisterLibFunc(&cgEventSetIntegerField, cg, "CGEventSetIntegerValueField")
	purego.RegisterLibFunc(&cgEventSourceCreate, cg, "CGEventSourceCreate")
	purego.RegisterLibFunc(&cgGetDisplaysWithPoint, cg, "CGGetDisplaysWithPoint")
	purego.RegisterLibFunc(&cgDisplayBounds, cg, "CGDisplayBounds")
	hidSource = cgEventSourceCreate(cgEventSourceStateHIDSystemState)
	// The older CGEventCreateScrollWheelEvent is variadic; this one needs macOS 13.
	if _, err := purego.Dlsym(cg, "CGEventCreateScrollWheelEvent2"); err == nil {
		purego.RegisterLibFunc(&cgEventCreateScrollWheel2, cg, "CGEventCreateScrollWheelEvent2")
	}
	return &macPointer{}, nil
}

// onScreen keeps a point on a display: past the edge, it stops at the edge of
// the display the pointer is on (where the Dock and hot corners wait), as a
// real mouse does. Posted points off every screen are not a push to the edge.
func onScreen(to, from cgPoint) cgPoint {
	var display, count uint32
	if cgGetDisplaysWithPoint(to, 1, &display, &count) == 0 && count > 0 {
		return to
	}
	if cgGetDisplaysWithPoint(from, 1, &display, &count) != 0 || count == 0 {
		return from
	}
	b := cgDisplayBounds(display)
	to.X = math.Max(b.Origin.X, math.Min(b.Origin.X+b.Size.W-1, to.X))
	to.Y = math.Max(b.Origin.Y, math.Min(b.Origin.Y+b.Size.H-1, to.Y))
	return to
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
	from := location()
	at := onScreen(cgPoint{from.X + float64(dx), from.Y + float64(dy)}, from)
	kind, button := uint32(cgEventMouseMoved), uint32(0)
	switch p.down {
	case "left":
		kind = cgEventLeftDragged
	case "right":
		kind, button = cgEventRightDragged, 1
	case "middle":
		kind, button = cgEventOtherDragged, 2
	}
	event := cgEventCreateMouseEvent(hidSource, kind, at, button)
	if event != 0 {
		cgEventSetIntegerField(event, cgMouseEventDeltaX, int64(dx))
		cgEventSetIntegerField(event, cgMouseEventDeltaY, int64(dy))
	}
	return post(event)
}

func (p *macPointer) Click(button string) error {
	if err := allowed(); err != nil {
		return err
	}
	if err := p.press(button, true); err != nil {
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

// Press starts a drag: always a single click, or a drag right after a tap
// would be a double-click drag (selecting words, zooming a window).
func (p *macPointer) Press(button string) error { return p.press(button, false) }

func (p *macPointer) press(button string, counted bool) error {
	if err := allowed(); err != nil {
		return err
	}
	b := macButtons[button]
	p.down = button
	at := location()
	near := math.Abs(at.X-p.lastPoint.X) <= doubleClickPx && math.Abs(at.Y-p.lastPoint.Y) <= doubleClickPx
	if counted && button == p.lastButton && near && time.Since(p.lastAt) < doubleClickTime {
		p.clicks++
	} else {
		p.clicks = 1
	}
	p.lastButton, p.lastAt, p.lastPoint = button, time.Now(), at
	return post(p.withClicks(cgEventCreateMouseEvent(hidSource, b[0], at, b[2])))
}

func (p *macPointer) Release(button string) error {
	if err := allowed(); err != nil {
		return err
	}
	b := macButtons[button]
	if p.down == button {
		p.down = ""
	}
	return post(p.withClicks(cgEventCreateMouseEvent(hidSource, b[1], location(), b[2])))
}

func (p *macPointer) withClicks(event uintptr) uintptr {
	if event != 0 {
		cgEventSetIntegerField(event, cgMouseEventClickState, max(1, p.clicks))
	}
	return event
}

func (p *macPointer) Scroll(dx, dy int) error {
	if err := allowed(); err != nil {
		return err
	}
	if cgEventCreateScrollWheel2 == nil {
		return errors.New("scrolling from the touchpad needs macOS 13 or later")
	}
	// wheel1 is vertical (positive scrolls up), wheel2 horizontal.
	return post(cgEventCreateScrollWheel2(hidSource, cgScrollEventUnitLine, 2, int32(-dy), int32(-dx), 0))
}
