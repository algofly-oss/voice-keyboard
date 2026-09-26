package main

import (
	"fmt"
	"sync"
)

// pointer moves the computer's mouse pointer, for the web app's touchpad.
//
//	Move:   relative motion in pixels
//	Click:  "left", "right" or "middle"
//	Scroll: wheel steps; dy > 0 scrolls down (towards the end), dx > 0 right
type pointer interface {
	Move(dx, dy int) error
	Click(button string) error
	Scroll(dx, dy int) error
}

// The pointer device is opened on first use, so a computer that never gets
// touchpad input has no extra virtual device, and a failure cannot affect typing.
var (
	pointerOnce sync.Once
	thePointer  pointer
	pointerErr  error
)

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
	var err error
	switch m.Action {
	case "move":
		err = thePointer.Move(clamp(m.DX, 4000), clamp(m.DY, 4000))
	case "click":
		if m.Button != "left" && m.Button != "right" && m.Button != "middle" {
			err = fmt.Errorf("unknown button %q", m.Button)
		} else {
			err = thePointer.Click(m.Button)
		}
	case "scroll":
		err = thePointer.Scroll(clamp(m.DX, 100), clamp(m.DY, 100))
	default:
		err = fmt.Errorf("unknown pointer action %q", m.Action)
	}
	if err != nil {
		reportError("pointer %s failed: %v", m.Action, err)
	}
}

func clamp(v, limit int) int {
	return max(-limit, min(limit, v))
}
