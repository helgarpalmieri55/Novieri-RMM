//go:build windows

package install

import (
	"os"
	"reflect"
	"testing"
)

// The MSI path takes the same parent-first ownership path as the link
// installer (2026-09-29): with %ProgramData%\ITSuite refused, nothing under it
// is created or touched and no token file is written. Runs in the
// agent-windows CI job, which is elevated: applyInPlace connects to the real
// Service Control Manager, finds no its-agent service on the runner, and
// stops at the faked directory refusal.
func TestTheMsiPathSecuresTheDataDirectorysParentFirst(t *testing.T) {
	calls, _ := fakeDirs(t, `C:\ProgramData\ITSuite`)
	if err := applyInPlace(dirsPlan, msiCode); err == nil {
		t.Fatal("applyInPlace went on after the parent was refused")
	}
	want := []dirCall{
		{"check", `C:\Program Files\ITSuite`, true},
		{"mkdir", `C:\Program Files\ITSuite`, false},
		{"secure", `C:\ProgramData\ITSuite`, true},
	}
	if !reflect.DeepEqual(*calls, want) {
		t.Fatalf("directory operations = %+v\nwant %+v", *calls, want)
	}
	if _, err := os.Stat(dirsPlan.TokenPath); !os.IsNotExist(err) {
		t.Fatalf("a token file exists after a refused parent: %v", err)
	}
}
