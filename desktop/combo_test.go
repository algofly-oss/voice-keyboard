package main

import "testing"

func TestParseCombo(t *testing.T) {
	for name, want := range map[string]*combo{
		"ctrl+c":           {ctrl: true, key: "c"},
		"alt+tab":          {alt: true, key: "tab"},
		"ctrl+shift+left":  {ctrl: true, shift: true, key: "left"},
		"shift+alt+ctrl+7": {ctrl: true, alt: true, shift: true, key: "7"},
		"enter":            nil, // a plain key
		"ctrl+":            nil,
		"meta+c":           nil,
		"ctrl+C":           nil, // letters are lower-case
		"ctrl+%":           nil,
		"ctrl+pageup":      nil,
		"alt+f4":           {alt: true, key: "f4"},
		"ctrl+shift+f12":   {ctrl: true, shift: true, key: "f12"},
		"alt+f13":          nil,
	} {
		got, ok := parseCombo(name)
		if want == nil {
			if ok {
				t.Errorf("%q: got %+v, want not a combination", name, got)
			}
		} else if !ok || got != *want {
			t.Errorf("%q: got %+v (%v), want %+v", name, got, ok, *want)
		}
	}
}
