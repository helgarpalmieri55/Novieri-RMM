//go:build windows

package install

import (
	"errors"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"golang.org/x/sys/windows"

	"github.com/toeshee/it-suite/agent/internal/secdir"
)

// The data directory's PARENT (2026-09-29). %ProgramData%\ITSuite was never
// protected: a standard user can create it before the install (Users may
// create folders in C:\ProgramData, and CREATOR OWNER gets Full Control on
// them), and owning it is FILE_DELETE_CHILD -- after the install they rename
// the protected `agent` directory away and put their own in its place, with
// an identity.json, pins.json and agent.key that point the SYSTEM service at
// their gateway and their job-signing key. prepareDirs secures ITSuite before
// `agent` is created or touched.
//
// These run in the agent-windows CI job (go test ./... on windows-latest).

type dirCall struct {
	op, dir string
	accept  bool // a non-nil owner check was passed
}

// fakeDirs replaces the three directory seams and records the calls, in
// order. failOn makes the named directory's secure/check fail.
func fakeDirs(t *testing.T, failOn string) (*[]dirCall, *map[string]func(*windows.SID) error) {
	t.Helper()
	var calls []dirCall
	accepts := map[string]func(*windows.SID) error{}
	boom := errors.New("refused by the fake")
	prevSecure, prevCheck, prevMkdir := secureOwned, checkOwned, mkdirAll
	secureOwned = func(dir string, accept func(*windows.SID) error) error {
		calls = append(calls, dirCall{"secure", dir, accept != nil})
		accepts[dir] = accept
		if dir == failOn {
			return boom
		}
		return nil
	}
	checkOwned = func(dir string, accept func(*windows.SID) error) error {
		calls = append(calls, dirCall{"check", dir, accept != nil})
		accepts[dir] = accept
		if dir == failOn {
			return boom
		}
		return nil
	}
	mkdirAll = func(dir string, _ os.FileMode) error {
		calls = append(calls, dirCall{"mkdir", dir, false})
		return nil
	}
	t.Cleanup(func() { secureOwned, checkOwned, mkdirAll = prevSecure, prevCheck, prevMkdir })
	return &calls, &accepts
}

var dirsPlan = NewPlan(`C:\Program Files`, `C:\ProgramData`, "https://itms.toeshee.com")

func TestTheDataDirectorysParentIsSecuredBeforeTheDataDirectory(t *testing.T) {
	calls, _ := fakeDirs(t, "")
	if err := prepareDirs(dirsPlan, true); err != nil {
		t.Fatal(err)
	}
	want := []dirCall{
		{"check", `C:\Program Files\ITSuite`, true},
		{"mkdir", `C:\Program Files\ITSuite`, false},
		{"secure", `C:\ProgramData\ITSuite`, true},
		{"secure", `C:\ProgramData\ITSuite\agent`, true},
	}
	if !reflect.DeepEqual(*calls, want) {
		t.Fatalf("directory operations = %+v\nwant %+v", *calls, want)
	}
}

func TestARefusedParentStopsBeforeTheDataDirectoryIsTouched(t *testing.T) {
	calls, _ := fakeDirs(t, `C:\ProgramData\ITSuite`)
	if err := prepareDirs(dirsPlan, true); err == nil {
		t.Fatal("prepareDirs went on after the parent was refused")
	}
	for _, c := range *calls {
		if c.dir == `C:\ProgramData\ITSuite\agent` {
			t.Fatalf("the data directory was touched after its parent was refused: %+v", *calls)
		}
	}
}

func TestAFreshInstallRefusesAForeignOwnerOnEveryDirectory(t *testing.T) {
	_, accepts := fakeDirs(t, "")
	if err := prepareDirs(dirsPlan, true); err != nil {
		t.Fatal(err)
	}
	users, err := windows.CreateWellKnownSid(windows.WinBuiltinUsersSid)
	if err != nil {
		t.Fatal(err)
	}
	ba, _ := windows.CreateWellKnownSid(windows.WinBuiltinAdministratorsSid)
	for _, dir := range []string{`C:\Program Files\ITSuite`, `C:\ProgramData\ITSuite`, `C:\ProgramData\ITSuite\agent`} {
		accept := (*accepts)[dir]
		if accept == nil {
			t.Fatalf("%s: no owner check on a fresh install", dir)
		}
		if err := accept(users); err == nil || !strings.Contains(err.Error(), "not to an administrator") {
			t.Fatalf("%s: a directory owned by BUILTIN\\Users was accepted (%v)", dir, err)
		}
		if err := accept(ba); err != nil {
			t.Fatalf("%s: a directory owned by BUILTIN\\Administrators was refused: %v", dir, err)
		}
	}
}

// On a re-install the directories are the previous install's; they are
// secured (owner and DACL taken), not refused.
func TestAReinstallSecuresWithoutAnOwnerCheck(t *testing.T) {
	calls, _ := fakeDirs(t, "")
	if err := prepareDirs(dirsPlan, false); err != nil {
		t.Fatal(err)
	}
	for _, c := range *calls {
		if c.accept {
			t.Fatalf("a re-install passed an owner check: %+v", *calls)
		}
	}
	if len(*calls) != 4 || (*calls)[2].dir != `C:\ProgramData\ITSuite` || (*calls)[3].dir != `C:\ProgramData\ITSuite\agent` {
		t.Fatalf("a re-install did not secure the parent and then the data directory: %+v", *calls)
	}
}

func TestOwnerAllowed(t *testing.T) {
	for _, k := range []windows.WELL_KNOWN_SID_TYPE{windows.WinBuiltinAdministratorsSid, windows.WinLocalSystemSid} {
		sid, _ := windows.CreateWellKnownSid(k)
		if err := ownerAllowed(`C:\ProgramData\ITSuite`, sid); err != nil {
			t.Errorf("%s refused: %v", sid, err)
		}
	}
	if u, err := windows.GetCurrentProcessToken().GetTokenUser(); err == nil {
		if err := ownerAllowed(`C:\ProgramData\ITSuite`, u.User.Sid); err != nil {
			t.Errorf("the installing user refused: %v", err)
		}
	}
	for _, k := range []windows.WELL_KNOWN_SID_TYPE{windows.WinBuiltinUsersSid, windows.WinLocalServiceSid, windows.WinAuthenticatedUserSid} {
		sid, _ := windows.CreateWellKnownSid(k)
		if err := ownerAllowed(`C:\ProgramData\ITSuite`, sid); err == nil {
			t.Errorf("%s accepted as an owner", sid)
		}
	}
	if err := ownerAllowed(`C:\ProgramData\ITSuite`, nil); err == nil {
		t.Error("an unreadable owner was accepted")
	}
}

// ---- against the real file system, with the real secdir ----------------

func realPlan(t *testing.T) Plan {
	t.Helper()
	if !windows.GetCurrentProcessToken().IsElevated() {
		t.Skip("assigning BUILTIN\\Administrators as owner needs an elevated token")
	}
	if reflect.ValueOf(secureOwned).Pointer() != reflect.ValueOf(secdir.SecureOwned).Pointer() {
		t.Fatal("secureOwned is not secdir.SecureOwned")
	}
	base := t.TempDir()
	for _, d := range []string{"PF", "PD"} {
		if err := os.Mkdir(filepath.Join(base, d), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	return NewPlan(filepath.Join(base, "PF"), filepath.Join(base, "PD"), "https://itms.toeshee.com")
}

func ownerSID(t *testing.T, dir string) *windows.SID {
	t.Helper()
	sd, err := windows.GetNamedSecurityInfo(dir, windows.SE_FILE_OBJECT, windows.OWNER_SECURITY_INFORMATION)
	if err != nil {
		t.Fatal(err)
	}
	o, _, err := sd.Owner()
	if err != nil {
		t.Fatal(err)
	}
	return o
}

// The attack, on a real directory: ITSuite pre-created and owned by a
// non-administrator (LOCAL SERVICE stands in for the standard user; icacls
// assigns it with the SeRestorePrivilege an elevated token holds).
func TestAForeignOwnedParentIsRefusedOnAFreshInstallAndLeftAlone(t *testing.T) {
	p := realPlan(t)
	parent := filepath.Dir(p.DataDir)
	if err := os.Mkdir(parent, 0o755); err != nil {
		t.Fatal(err)
	}
	if out, err := exec.Command("icacls", parent, "/setowner", "*S-1-5-19").CombinedOutput(); err != nil {
		t.Fatalf("icacls /setowner: %v\n%s", err, out)
	}
	err := prepareDirs(p, true)
	if err == nil || !strings.Contains(err.Error(), "not to an administrator") {
		t.Fatalf("prepareDirs = %v; want the foreign-owned parent refused", err)
	}
	if _, err := os.Stat(p.DataDir); !os.IsNotExist(err) {
		t.Fatalf("the data directory was created under a refused parent: %v", err)
	}
	ls, _ := windows.CreateWellKnownSid(windows.WinLocalServiceSid)
	if !ownerSID(t, parent).Equals(ls) {
		t.Fatal("a refused parent was taken over; it must be left for an administrator to inspect")
	}

	// A re-install (the service exists) takes it over instead.
	if err := prepareDirs(p, false); err != nil {
		t.Fatalf("prepareDirs on a re-install: %v", err)
	}
	ba, _ := windows.CreateWellKnownSid(windows.WinBuiltinAdministratorsSid)
	for _, d := range []string{parent, p.DataDir} {
		if !ownerSID(t, d).Equals(ba) {
			t.Fatalf("%s is not owned by Administrators after a re-install", d)
		}
	}
}

func TestAParentThatIsAJunctionIsRefused(t *testing.T) {
	p := realPlan(t)
	parent := filepath.Dir(p.DataDir)
	elsewhere := filepath.Join(filepath.Dir(filepath.Dir(parent)), "elsewhere")
	if err := os.Mkdir(elsewhere, 0o755); err != nil {
		t.Fatal(err)
	}
	if out, err := exec.Command("cmd", "/c", "mklink", "/J", parent, elsewhere).CombinedOutput(); err != nil {
		t.Fatalf("mklink /J: %v\n%s", err, out)
	}
	for _, fresh := range []bool{true, false} {
		err := prepareDirs(p, fresh)
		if err == nil || !strings.Contains(err.Error(), "junction or symbolic link") {
			t.Fatalf("prepareDirs(fresh=%v) = %v; want the junction refused", fresh, err)
		}
		if _, err := os.Stat(filepath.Join(elsewhere, "agent")); !os.IsNotExist(err) {
			t.Fatalf("a data directory was created through the junction: %v", err)
		}
	}
}

func TestAFreshInstallCreatesBothDirectoriesOwned(t *testing.T) {
	p := realPlan(t)
	if err := prepareDirs(p, true); err != nil {
		t.Fatal(err)
	}
	ba, _ := windows.CreateWellKnownSid(windows.WinBuiltinAdministratorsSid)
	for _, d := range []string{filepath.Dir(p.DataDir), p.DataDir} {
		sd, err := windows.GetNamedSecurityInfo(d, windows.SE_FILE_OBJECT,
			windows.OWNER_SECURITY_INFORMATION|windows.DACL_SECURITY_INFORMATION)
		if err != nil {
			t.Fatal(err)
		}
		o, _, _ := sd.Owner()
		got := sd.String()
		if o == nil || !o.Equals(ba) || strings.Count(got, "(A;") != 2 ||
			(!strings.Contains(got, "D:P(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)") &&
				!strings.Contains(got, "D:PAI(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)")) {
			t.Fatalf("%s: %s; want owner BA and exactly the protected SYSTEM+Administrators DACL", d, got)
		}
	}
	if _, err := os.Stat(p.InstallDir); err != nil {
		t.Fatalf("the install directory was not created: %v", err)
	}
}
