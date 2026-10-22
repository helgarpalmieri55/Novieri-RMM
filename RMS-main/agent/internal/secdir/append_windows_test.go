//go:build windows

package secdir

import (
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"golang.org/x/sys/windows"
)

// AppendLine is the Group Policy MSI's msi.log writer. Executed by the
// agent-windows CI job, elevated; an unelevated run skips.

func TestAppendLineSecuresAnAbsentParentFirstThenAppends(t *testing.T) {
	needElevated(t)
	dir := filepath.Join(t.TempDir(), "ITSuite")
	for _, line := range []string{"first", "second"} {
		if err := AppendLine(dir, "msi.log", line, true); err != nil {
			t.Fatalf("AppendLine(%q): %v", line, err)
		}
	}
	assertOwned(t, dir)
	b, err := os.ReadFile(filepath.Join(dir, "msi.log"))
	if err != nil {
		t.Fatal(err)
	}
	if string(b) != "first\r\nsecond\r\n" {
		t.Fatalf("msi.log = %q", b)
	}
}

func TestAppendLineWithoutCreateLeavesAnAbsentParentAbsent(t *testing.T) {
	needElevated(t)
	dir := filepath.Join(t.TempDir(), "ITSuite")
	if err := AppendLine(dir, "msi.log", "x", false); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("err = %v, want os.ErrNotExist", err)
	}
	if _, err := os.Stat(dir); !os.IsNotExist(err) {
		t.Fatalf("the parent was created: %v", err)
	}
}

func TestAppendLineRefusesAParentSomeoneElseOwnsAndChangesNothing(t *testing.T) {
	needElevated(t)
	dir := filepath.Join(t.TempDir(), "ITSuite")
	if err := os.Mkdir(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	// LOCAL SERVICE: neither Administrators nor SYSTEM.
	if out, err := exec.Command("icacls", dir, "/setowner", "*S-1-5-19").CombinedOutput(); err != nil {
		t.Fatalf("icacls /setowner: %v\n%s", err, out)
	}
	if err := AppendLine(dir, "msi.log", "x", true); err == nil {
		t.Fatal("a directory LOCAL SERVICE owns was accepted")
	}
	if owner, _ := descriptor(t, dir); !owner.Equals(wellKnown(t, windows.WinLocalServiceSid)) {
		t.Fatalf("the owner was changed to %s", owner)
	}
	if _, err := os.Stat(filepath.Join(dir, "msi.log")); !os.IsNotExist(err) {
		t.Fatalf("msi.log was written: %v", err)
	}
}

func TestAppendLineRefusesAHardLinkedFile(t *testing.T) {
	needElevated(t)
	root := t.TempDir()
	dir := filepath.Join(root, "ITSuite")
	if err := MkdirOwned(dir); err != nil {
		t.Fatal(err)
	}
	elsewhere := filepath.Join(root, "elsewhere.txt")
	if err := os.WriteFile(elsewhere, []byte("keep\r\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Link(elsewhere, filepath.Join(dir, "msi.log")); err != nil {
		t.Fatal(err)
	}
	if err := AppendLine(dir, "msi.log", "x", true); err == nil || !strings.Contains(err.Error(), "hard link") {
		t.Fatalf("err = %v, want a hard-link refusal", err)
	}
	if b, _ := os.ReadFile(elsewhere); string(b) != "keep\r\n" {
		t.Fatalf("the linked file was written: %q", b)
	}
}

func TestAppendLineRefusesASymbolicLink(t *testing.T) {
	needElevated(t)
	root := t.TempDir()
	dir := filepath.Join(root, "ITSuite")
	if err := MkdirOwned(dir); err != nil {
		t.Fatal(err)
	}
	elsewhere := filepath.Join(root, "elsewhere.txt")
	if err := os.WriteFile(elsewhere, []byte("keep\r\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(elsewhere, filepath.Join(dir, "msi.log")); err != nil {
		t.Skipf("cannot create a symbolic link here: %v", err)
	}
	if err := AppendLine(dir, "msi.log", "x", true); err == nil {
		t.Fatal("a symbolic link was written through")
	}
	if b, _ := os.ReadFile(elsewhere); string(b) != "keep\r\n" {
		t.Fatalf("the link's target was written: %q", b)
	}
}
