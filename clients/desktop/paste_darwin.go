package main

import (
	"errors"
	"os"
	"os/exec"
	"strings"
	"time"
)

// Pasteboard types that are plain text. Anything else on the clipboard (an
// image, files, rich text) means pasting stands down, so it is never altered.
var plainTextTypes = map[string]bool{
	"«class ut16»": true, "«class utf8»": true, "string": true, "Unicode text": true,
	"«class ustr»": true, "«class TEXT»": true, "«class ktxt»": true, "«class ukst»": true,
}

func pasteText(_ keyboard, text string) error {
	if err := load(); err != nil {
		return err
	}
	info, err := exec.Command("osascript", "-e", "clipboard info").Output()
	if err != nil {
		return err
	}
	parts := strings.Split(strings.TrimSpace(string(info)), ", ")
	for i := 0; i+1 < len(parts); i += 2 { // "type, size, type, size, …"
		if !plainTextTypes[parts[i]] {
			return errors.New("the clipboard holds more than plain text")
		}
	}
	saved, err := clipboardCommand("pbpaste", "").Output()
	if err != nil {
		return err
	}
	if err := clipboardCommand("pbcopy", text).Run(); err != nil {
		return err
	}
	const vKey, commandFlag = 9, 0x100000
	for _, down := range []bool{true, false} {
		event := cgEventCreateKeyboardEvent(0, vKey, down)
		cgEventSetFlags(event, commandFlag)
		cgEventPost(cgHIDEventTap, event)
		cfRelease(event)
	}
	time.Sleep(250 * time.Millisecond) // let the app read the clipboard before it is restored
	return clipboardCommand("pbcopy", string(saved)).Run()
}

// clipboardCommand runs pbcopy/pbpaste in UTF-8: a login item has no locale,
// and they would otherwise mangle anything beyond ASCII.
func clipboardCommand(name, input string) *exec.Cmd {
	cmd := exec.Command(name)
	cmd.Env = append(os.Environ(), "LANG=en_US.UTF-8", "LC_ALL=en_US.UTF-8")
	if name == "pbcopy" {
		cmd.Stdin = strings.NewReader(input)
	}
	return cmd
}
