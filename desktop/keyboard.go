package main

import (
	"errors"
	"fmt"
	"log"
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
