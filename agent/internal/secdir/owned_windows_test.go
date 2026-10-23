//go:build windows

package secdir

import (
	"strings"
	"testing"

	"golang.org/x/sys/windows"
)

// RestrictOwned is what the install link's self-installer relied on (review
// I4) before SecureOwned, which does the same through a handle that does not
// follow a junction (secure_windows_test.go): the DACL alone is not
// enough when the directory could have been PRE-CREATED by a local user, who
// then owns it -- and an owner can always rewrite a DACL (WRITE_DAC is implied
// for the owner). Setting the owner to BUILTIN\Administrators closes that.
//
// Executed by the agent-windows CI job (go test ./... on windows-latest),
// which runs elevated; an unelevated run cannot assign an owner and skips.
func TestRestrictOwnedSetsTheOwnerToAdministratorsAndTheProtectedDACL(t *testing.T) {
	if !windows.GetCurrentProcessToken().IsElevated() {
		t.Skip("assigning BUILTIN\\Administrators as owner needs an elevated token")
	}
	dir := t.TempDir()
	if err := RestrictOwned(dir); err != nil {
		t.Fatalf("RestrictOwned: %v", err)
	}
	sd, err := windows.GetNamedSecurityInfo(dir, windows.SE_FILE_OBJECT,
		windows.OWNER_SECURITY_INFORMATION|windows.DACL_SECURITY_INFORMATION)
	if err != nil {
		t.Fatal(err)
	}
	owner, _, err := sd.Owner()
	if err != nil {
		t.Fatal(err)
	}
	ba, err := windows.CreateWellKnownSid(windows.WinBuiltinAdministratorsSid)
	if err != nil {
		t.Fatal(err)
	}
	if !owner.Equals(ba) {
		t.Fatalf("owner = %s, want BUILTIN\\Administrators (%s)", owner, ba)
	}
	got := sd.String()
	if !strings.Contains(got, "D:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)") {
		t.Fatalf("security descriptor = %s, want exactly SYSTEM and Administrators, protected", got)
	}
}

func TestMkdirOwnedCreatesTheDirectoryAlreadyOwnedAndRestricted(t *testing.T) {
	if !windows.GetCurrentProcessToken().IsElevated() {
		t.Skip("assigning BUILTIN\\Administrators as owner needs an elevated token")
	}
	dir := t.TempDir() + `\uninstall-1`
	if err := MkdirOwned(dir); err != nil {
		t.Fatalf("MkdirOwned: %v", err)
	}
	if err := MkdirOwned(dir); err == nil {
		t.Fatal("MkdirOwned succeeded over an existing directory; a pre-created one must be refused")
	}
	sd, err := windows.GetNamedSecurityInfo(dir, windows.SE_FILE_OBJECT,
		windows.OWNER_SECURITY_INFORMATION|windows.DACL_SECURITY_INFORMATION)
	if err != nil {
		t.Fatal(err)
	}
	owner, _, _ := sd.Owner()
	ba, _ := windows.CreateWellKnownSid(windows.WinBuiltinAdministratorsSid)
	if owner == nil || !owner.Equals(ba) {
		t.Fatalf("owner = %v, want BUILTIN\\Administrators", owner)
	}
	// "D:P" and not "D:PAI": a DACL handed to CreateDirectory is stored as
	// given, and SE_DACL_AUTO_INHERITED ("AI") is only set when the system
	// applies a DACL to an EXISTING object (SetNamedSecurityInfo, which is
	// Restrict's and RestrictOwned's path). What matters is identical in both:
	// PROTECTED from the parent, and exactly SYSTEM and Administrators, full
	// control, inherited by what is created inside. (First run on
	// windows-latest read "O:BAD:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)".)
	got := sd.String()
	if !strings.Contains(got, "D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)") &&
		!strings.Contains(got, "D:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)") {
		t.Fatalf("security descriptor = %s, want exactly SYSTEM and Administrators, protected", got)
	}
	if strings.Count(got, "(A;") != 2 {
		t.Fatalf("security descriptor = %s, want exactly two ACEs", got)
	}
}

func TestTheOwnedDescriptorParses(t *testing.T) {
	sd, err := windows.SecurityDescriptorFromString(ownedSDDL)
	if err != nil {
		t.Fatal(err)
	}
	if _, _, err := sd.Owner(); err != nil {
		t.Fatalf("no owner in %q: %v", ownedSDDL, err)
	}
}
