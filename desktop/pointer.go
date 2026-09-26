package main

import (
	"fmt"
	"sync"
	"time"
)

// pointer moves the computer's mouse pointer, for the web app's touchpad.
//
//	Move:    relative motion in pixels (a drag while a button is held)
//	Click:   "left", "right" or "middle"
//	Press / Release: hold a button down and let it go (dragging)
//	Scroll:  wheel steps; dy > 0 scrolls down (towards the end), dx > 0 right
type pointer interface {
	Move(dx, dy int) error
	Click(button string) error
	Press(button string) error
	Release(button string) error
	Scroll(dx, dy int) error
}

// The pointer device is opened on first use, so a computer that never gets
// touchpad input has no extra virtual device, and a failure cannot affect typing.
var (
	pointerOnce sync.Once
	thePointer  pointer
	pointerErr  error
)

// held: buttons pressed for a drag. If the release never comes (the phone lost
// its connection mid-drag), they are let go after a few seconds without input.
var (
	heldMu      sync.Mutex
	held        = map[string]bool{}
	heldTimeout *time.Timer
)

const dragTimeout = 5 * time.Second

func handlePointer(m message) {
	pointerOnce.Do(func() {
		thePointer, pointerErr = newPointer()
		if pointerErr != nil {
			reportError("touchpad unavailable: %v", pointerErr)
		}
	})
	if pointerErr != nil {
		return
	}
	valid := m.Button == "left" || m.Button == "right" || m.Button == "middle"
	var err error
	switch m.Action {
	case "move":
		err = thePointer.Move(clamp(m.DX, 4000), clamp(m.DY, 4000))
	case "click":
		if !valid {
			err = fmt.Errorf("unknown button %q", m.Button)
		} else {
			err = thePointer.Click(m.Button)
		}
	case "press", "release":
		if !valid {
			err = fmt.Errorf("unknown button %q", m.Button)
		} else {
			err = setHeld(m.Button, m.Action == "press")
		}
	case "scroll":
		err = thePointer.Scroll(clamp(m.DX, 100), clamp(m.DY, 100))
	default:
		err = fmt.Errorf("unknown pointer action %q", m.Action)
	}
	if err != nil {
		reportError("pointer %s failed: %v", m.Action, err)
	}
	heldMu.Lock()
	if heldTimeout != nil && len(held) > 0 {
		heldTimeout.Reset(dragTimeout) // any input keeps a drag going
	}
	heldMu.Unlock()
}

func setHeld(button string, down bool) error {
	heldMu.Lock()
	defer heldMu.Unlock()
	if held[button] == down {
		return nil
	}
	var err error
	if down {
		err = thePointer.Press(button)
		held[button] = true
		if heldTimeout == nil {
			heldTimeout = time.AfterFunc(dragTimeout, releaseAll)
		} else {
			heldTimeout.Reset(dragTimeout)
		}
	} else {
		err = thePointer.Release(button)
		delete(held, button)
	}
	return err
}

func releaseAll() {
	heldMu.Lock()
	defer heldMu.Unlock()
	for button := range held {
		if err := thePointer.Release(button); err != nil {
			reportError("pointer release failed: %v", err)
		}
		delete(held, button)
	}
}

func clamp(v, limit int) int {
	return max(-limit, min(limit, v))
}
