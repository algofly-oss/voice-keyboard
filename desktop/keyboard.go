package main

import (
	"errors"
	"fmt"
	"log"
	"slices"
	"strings"
	"sync"
	"time"
	"unicode/utf8"
)

// configuredDelay overrides each backend's pause between characters when set
// (from `vkeyboard config --delay-ms`); negative means the platform default.
var configuredDelay = -1

// pause waits between characters: platformDefault unless configured otherwise.
func pause(platformDefault time.Duration) {
	d := platformDefault
	if configuredDelay >= 0 {
		d = time.Duration(configuredDelay) * time.Millisecond
	}
	if d > 0 {
		time.Sleep(d)
	}
}

// pasteThreshold: shorter pieces are typed, since saving and restoring the
// clipboard costs more than typing a few dozen characters.
const pasteThreshold = 80

var errPasteUnsupported = errors.New("paste is not available on this system")

// pasteKeyboard pastes long pieces through the clipboard and restores the
// user's clipboard afterwards. pasteText refuses (and the text is typed) when
// the clipboard holds anything but plain text, so it is never altered.
type pasteKeyboard struct {
	keyboard
	warnOnce sync.Once
}

func (p *pasteKeyboard) Type(text string) error {
	if utf8.RuneCountInString(text) >= pasteThreshold {
		err := pasteText(p.keyboard, text)
		if err == nil {
			return nil
		}
		if errors.Is(err, errPasteUnsupported) {
			p.warnOnce.Do(func() { log.Printf("%v; typing instead", err) })
		} else {
			log.Printf("pasting skipped (%v); typing instead", err)
		}
	}
	return p.keyboard.Type(text)
}

// newConfiguredKeyboard applies the user's typing preferences.
func newConfiguredKeyboard(cfg config) (keyboard, error) {
	if cfg.DelayMs != nil {
		configuredDelay = *cfg.DelayMs
	}
	kb, err := newKeyboard()
	if err != nil || cfg.Method != "paste" {
		return kb, err
	}
	return &pasteKeyboard{keyboard: kb}, nil
}

// keyboard types into whichever application has focus.
type keyboard interface {
	// Type enters text verbatim; "\n" presses Enter.
	Type(text string) error
	// Key sends a named key (backspace, enter, up, down, left, right, escape,
	// tab, space, f1–f12) or a combination such as "ctrl+c", "alt+tab" or
	// "ctrl+shift+left": modifiers ctrl, alt and shift, then a named key, a
	// letter or a digit. state is "press" (down and up), "down"/"hold", or "up";
	// a combination is always pressed as a whole.
	Key(name, state string) error
	Close()
}

var keyNames = []string{"backspace", "enter", "up", "down", "left", "right", "escape", "tab", "space",
	"f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8", "f9", "f10", "f11", "f12"}

// combo is a key pressed with modifiers.
type combo struct {
	ctrl, alt, shift bool
	key              string // a named key, or one letter or digit
}

// parseCombo reads "ctrl+alt+shift+<key>"; false for a plain key name.
func parseCombo(name string) (combo, bool) {
	parts := strings.Split(name, "+")
	if len(parts) < 2 {
		return combo{}, false
	}
	var c combo
	for _, m := range parts[:len(parts)-1] {
		switch m {
		case "ctrl":
			c.ctrl = true
		case "alt":
			c.alt = true
		case "shift":
			c.shift = true
		default:
			return combo{}, false
		}
	}
	c.key = parts[len(parts)-1]
	if len(c.key) == 1 {
		k := c.key[0]
		return c, k >= 'a' && k <= 'z' || k >= '0' && k <= '9'
	}
	return c, slices.Contains(keyNames, c.key)
}

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
