package main

// Pasting would need a clipboard owner process (or xclip/wl-copy), and
// terminals paste with Ctrl+Shift+V instead of Ctrl+V; typing through XTest
// is already instant on X11, so Linux keeps typing.
func pasteText(keyboard, string) error { return errPasteUnsupported }
