//go:build windows

package deploy

import (
	"testing"

	"golang.org/x/sys/windows"
)

// TestRestrictDirAppliesAProtectedTwoEntryDACL is restrictDir's own falsifier,
// run against a real NTFS directory and read back through the real Windows
// security API -- not asserted from the SDDL string, which would only prove
// that scratchSDDL parses.
//
// Until this job started running on windows-latest, perm_windows.go's own
// comment was accurate: "NEVER EXECUTED IN CI". It now is. This does not
// prove the ACEs it applies are correct for a deployed endpoint (that needs
// the `icacls` check the comment names, against a real domain-joined
// Windows 11 box), but it does prove the call succeeds against a real
// filesystem and that the security descriptor Windows hands back is the
// shape restrictDir claims: a protected DACL with exactly the two ACEs
// scratchSDDL names.
func TestRestrictDirAppliesAProtectedTwoEntryDACL(t *testing.T) {
	dir := t.TempDir()

	if err := restrictDir(dir); err != nil {
		t.Fatalf("restrictDir: %v", err)
	}

	sd, err := windows.GetNamedSecurityInfo(dir, windows.SE_FILE_OBJECT, windows.DACL_SECURITY_INFORMATION)
	if err != nil {
		t.Fatalf("GetNamedSecurityInfo: %v", err)
	}
	control, _, err := sd.Control()
	if err != nil {
		t.Fatalf("reading back the security descriptor's control bits: %v", err)
	}
	if control&windows.SE_DACL_PROTECTED == 0 {
		// FALSIFYING OBSERVATION: the protected bit absent, which is exactly
		// the "looks correct in the source and is wrong on the box" failure
		// mode the comment above restrictDir names -- inheritance from a
		// widened %TEMP% would still apply.
		t.Fatal("the DACL restrictDir wrote is not protected; a wider ACE inherited from the " +
			"parent directory would still apply")
	}

	dacl, _, err := sd.DACL()
	if err != nil {
		t.Fatalf("reading back the DACL: %v", err)
	}
	if dacl == nil {
		t.Fatal("restrictDir left a nil DACL, which Windows treats as \"everyone, full access\"")
	}
	if dacl.AceCount != 2 {
		t.Fatalf("the applied DACL has %d ACEs, want exactly 2 (SYSTEM, Administrators) -- an "+
			"extra ACE here is an extra principal that can read a staged script or installer",
			dacl.AceCount)
	}
}
