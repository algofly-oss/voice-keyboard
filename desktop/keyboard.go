package main

import "fmt"

// keyboard types into whichever application has focus.
type keyboard interface {
	// Type enters text verbatim; "\n" presses Enter.
	Type(text string) error
	// Key sends a named key: backspace, enter, up, down, left, right.
	// state is "press" (down and up), "down"/"hold", or "up".
	Key(name, state string) error
	Close()
}

var keyNames = []string{"backspace", "enter", "up", "down", "left", "right"}

// pressKey applies a press/down/up state using a platform's down/up primitive.
func pressKey(send func(down bool) error, state string) error {
	switch state {
	case "", "press":
		if err := send(true); err != nil {
			return err
		}
		return send(false)
	case "down", "hold":
		return send(true)
	case "up":
		return send(false)
	}
	return fmt.Errorf("unknown key state %q", state)
}
