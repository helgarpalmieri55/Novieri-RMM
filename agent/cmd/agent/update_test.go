package main

import (
	"path/filepath"
	"testing"
)

// The helper takes a phase and one path and nothing else; anything else is
// refused before a file is opened.
func TestRunHelperRefusesAnythingButItsOwnArguments(t *testing.T) {
	good := filepath.Join(t.TempDir(), "update", "state.json")
	for _, c := range []struct{ phase, path string }{
		{"install", good},
		{"", good},
		{"apply", filepath.Join(t.TempDir(), "state.json")},
		{"apply", filepath.Join(t.TempDir(), "update", "other.json")},
	} {
		if code := runHelper(c.phase, c.path); code != 2 {
			t.Errorf("runHelper(%q, %q) = %d, want 2", c.phase, c.path, code)
		}
	}
}
