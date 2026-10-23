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

// SecureOwned and CheckOwned are what the self-installer applies to
// %ProgramData%\ITSuite -- the data directory's PARENT -- and to the data
// directory itself, and what the uninstall helper applies to ITSuite before it
// stages its copy there. A standard user can create C:\ProgramData\ITSuite
// before the install (Users may create folders in ProgramData; CREATOR OWNER
// gets Full Control) and, owning it, rename the protected `agent` directory
// away and put their own in its place.
//
// Executed by the agent-windows CI job (go test ./... on windows-latest),
// which runs elevated; an unelevated run cannot assign an owner and skips.

func needElevated(t *testing.T) {
	t.Helper()
	if !windows.GetCurrentProcessToken().IsElevated() {
		t.Skip("assigning BUILTIN\\Administrators as owner needs an elevated token")
	}
}

func descriptor(t *testing.T, dir string) (owner *windows.SID, sddl string) {
	t.Helper()
	sd, err := windows.GetNamedSecurityInfo(dir, windows.SE_FILE_OBJECT,
		windows.OWNER_SECURITY_INFORMATION|windows.DACL_SECURITY_INFORMATION)
	if err != nil {
		t.Fatal(err)
	}
	owner, _, err = sd.Owner()
	if err != nil {
		t.Fatal(err)
	}
	return owner, sd.String()
}

func wellKnown(t *testing.T, k windows.WELL_KNOWN_SID_TYPE) *windows.SID {
	t.Helper()
	sid, err := windows.CreateWellKnownSid(k)
	if err != nil {
		t.Fatal(err)
	}
	return sid
}

// assertOwned: owner BA and exactly the protected SYSTEM+Administrators DACL.
// "D:P" for a directory created with it, "D:PAI" for one it was applied to
// (see TestMkdirOwnedCreatesTheDirectoryAlreadyOwnedAndRestricted).
func assertOwned(t *testing.T, dir string) {
	t.Helper()
	owner, got := descriptor(t, dir)
	if !owner.Equals(wellKnown(t, windows.WinBuiltinAdministratorsSid)) {
		t.Fatalf("%s: owner = %s, want BUILTIN\\Administrators", dir, owner)
	}
	if !strings.Contains(got, "D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)") &&
		!strings.Contains(got, "D:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)") {
		t.Fatalf("%s: security descriptor = %s, want exactly SYSTEM and Administrators, protected", dir, got)
	}
	if strings.Count(got, "(A;") != 2 {
		t.Fatalf("%s: security descriptor = %s, want exactly two ACEs", dir, got)
	}
}

// junction makes `link` a directory junction to `target` -- the reparse point
// a standard user can create without any privilege.
func junction(t *testing.T, link, target string) {
	t.Helper()
	if out, err := exec.Command("cmd", "/c", "mklink", "/J", link, target).CombinedOutput(); err != nil {
		t.Fatalf("mklink /J: %v\n%s", err, out)
	}
}

func TestSecureOwnedCreatesAnAbsentDirectoryOwnedAndRestricted(t *testing.T) {
	needElevated(t)
	dir := filepath.Join(t.TempDir(), "ITSuite")
	if err := SecureOwned(dir, nil); err != nil {
		t.Fatalf("SecureOwned: %v", err)
	}
	assertOwned(t, dir)
}

func TestSecureOwnedTakesAnExistingDirectory(t *testing.T) {
	needElevated(t)
	dir := filepath.Join(t.TempDir(), "ITSuite")
	if err := os.Mkdir(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	// Owned by someone else, as a directory a standard user pre-created is.
	// icacls enables SeRestorePrivilege, which an elevated token holds.
	if out, err := exec.Command("icacls", dir, "/setowner", "*S-1-5-19").CombinedOutput(); err != nil {
		t.Fatalf("icacls /setowner: %v\n%s", err, out)
	}
	if err := SecureOwned(dir, nil); err != nil {
		t.Fatalf("SecureOwned: %v", err)
	}
	assertOwned(t, dir)
}

func TestSecureOwnedAsksBeforeItChangesAnything(t *testing.T) {
	needElevated(t)
	dir := filepath.Join(t.TempDir(), "ITSuite")
	if err := os.Mkdir(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	if out, err := exec.Command("icacls", dir, "/setowner", "*S-1-5-19").CombinedOutput(); err != nil {
		t.Fatalf("icacls /setowner: %v\n%s", err, out)
	}
	_, before := descriptor(t, dir)
	refused := errors.New("not yours")
	var shown *windows.SID
	err := SecureOwned(dir, func(owner *windows.SID) error {
		shown = owner
		return refused
	})
	if !errors.Is(err, refused) {
		t.Fatalf("SecureOwned = %v; want the refusal", err)
	}
	if shown == nil || !shown.Equals(wellKnown(t, windows.WinLocalServiceSid)) {
		t.Fatalf("accept was shown owner %v; want LOCAL SERVICE, the directory's real owner", shown)
	}
	if _, after := descriptor(t, dir); after != before {
		t.Fatalf("a refused directory was changed:\n before %s\n after  %s", before, after)
	}
}

func TestSecureOwnedDoesNotAskAboutADirectoryItCreated(t *testing.T) {
	needElevated(t)
	dir := filepath.Join(t.TempDir(), "ITSuite")
	if err := SecureOwned(dir, func(*windows.SID) error { return errors.New("asked") }); err != nil {
		t.Fatalf("SecureOwned consulted accept about a directory it created itself: %v", err)
	}
	assertOwned(t, dir)
}

func TestSecureOwnedRefusesAJunctionAndLeavesItsTargetAlone(t *testing.T) {
	needElevated(t)
	base := t.TempDir()
	target := filepath.Join(base, "elsewhere")
	if err := os.Mkdir(target, 0o755); err != nil {
		t.Fatal(err)
	}
	_, before := descriptor(t, target)
	link := filepath.Join(base, "ITSuite")
	junction(t, link, target)
	err := SecureOwned(link, nil)
	if err == nil || !strings.Contains(err.Error(), "junction or symbolic link") {
		t.Fatalf("SecureOwned on a junction = %v; want a refusal naming it", err)
	}
	if _, after := descriptor(t, target); after != before {
		t.Fatalf("the junction's target was changed:\n before %s\n after  %s", before, after)
	}
}

func TestSecureOwnedRefusesASymbolicLink(t *testing.T) {
	needElevated(t)
	base := t.TempDir()
	target := filepath.Join(base, "elsewhere")
	if err := os.Mkdir(target, 0o755); err != nil {
		t.Fatal(err)
	}
	link := filepath.Join(base, "ITSuite")
	if err := os.Symlink(target, link); err != nil {
		t.Skipf("cannot create a directory symbolic link here: %v", err)
	}
	if err := SecureOwned(link, nil); err == nil || !strings.Contains(err.Error(), "junction or symbolic link") {
		t.Fatalf("SecureOwned on a symbolic link = %v; want a refusal naming it", err)
	}
}

func TestSecureOwnedRefusesAFile(t *testing.T) {
	needElevated(t)
	path := filepath.Join(t.TempDir(), "ITSuite")
	if err := os.WriteFile(path, nil, 0o644); err != nil {
		t.Fatal(err)
	}
	if err := SecureOwned(path, nil); err == nil || !strings.Contains(err.Error(), "not a directory") {
		t.Fatalf("SecureOwned on a file = %v; want a refusal", err)
	}
}

func TestCheckOwnedAcceptsAnAbsentDirectoryAndChangesNothing(t *testing.T) {
	dir := filepath.Join(t.TempDir(), "ITSuite")
	if err := CheckOwned(dir, func(*windows.SID) error { return errors.New("asked") }); err != nil {
		t.Fatalf("CheckOwned on an absent directory = %v", err)
	}
	if _, err := os.Stat(dir); !os.IsNotExist(err) {
		t.Fatalf("CheckOwned created %s", dir)
	}
}

func TestCheckOwnedShowsTheOwnerAndRefusesAJunction(t *testing.T) {
	needElevated(t)
	base := t.TempDir()
	dir := filepath.Join(base, "ITSuite")
	if err := os.Mkdir(dir, 0o755); err != nil {
		t.Fatal(err)
	}
	_, before := descriptor(t, dir)
	refused := errors.New("not yours")
	if err := CheckOwned(dir, func(*windows.SID) error { return refused }); !errors.Is(err, refused) {
		t.Fatalf("CheckOwned = %v; want the refusal", err)
	}
	if _, after := descriptor(t, dir); after != before {
		t.Fatalf("CheckOwned changed the directory:\n before %s\n after  %s", before, after)
	}
	link := filepath.Join(base, "link")
	junction(t, link, dir)
	if err := CheckOwned(link, nil); err == nil || !strings.Contains(err.Error(), "junction or symbolic link") {
		t.Fatalf("CheckOwned on a junction = %v; want a refusal naming it", err)
	}
}
