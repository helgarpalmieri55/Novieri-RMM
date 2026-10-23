package packaging_test

import (
	"strings"
	"testing"
)

// The PARENT of the data directory (install link, 2026-09-29). On a default
// Windows install BUILTIN\Users may create folders in C:\ProgramData, and
// CREATOR OWNER is granted Full Control on what they create. A standard user
// who runs `mkdir C:\ProgramData\ITSuite` before the first install therefore
// owns the data directory's parent, and owning a directory is
// FILE_DELETE_CHILD on it: after the install they rename the protected
// `agent` directory away and put their own in its place -- an identity.json
// and pins.json naming their gateway and their job-signing key, which the
// SYSTEM service then obeys. Protecting `agent` alone protects nothing.
//
// So both scripts that create the data directory secure `ITSuite` FIRST:
// owner BUILTIN\Administrators, the protected SYSTEM+Administrators DACL, a
// junction or symbolic link refused -- and only then create or touch `agent`.
// The self-installer does the same in Go (internal/install prepareDirs,
// secdir.SecureOwned); the install-link e2e reads the parent back on a real
// Windows runner.
func TestWindowsScriptsSecureTheDataDirectorysParentBeforeTheDataDirectory(t *testing.T) {
	for _, rel := range []string{"windows/install.ps1", "windows/its-agent-enroll.ps1"} {
		text := read(t, rel)
		code := stripPowerShellLiterals(text)

		fn := strings.Index(code, "function Protect-ItsDirectory")
		if fn < 0 {
			t.Errorf("%s defines no Protect-ItsDirectory", rel)
			continue
		}
		body := code[fn:]
		if end := strings.Index(body, "\n}"); end > 0 {
			body = body[:end]
		}
		for _, want := range []string{
			"SetAccessRuleProtection($true, $false)",
			"SetOwner(",
			"Assert-NotReparsePoint",
			"Set-Acl -LiteralPath $Path",
		} {
			if !strings.Contains(body, want) {
				t.Errorf("%s: Protect-ItsDirectory does not contain %q", rel, want)
			}
		}
		if !strings.Contains(text, "BUILTIN\\Administrators") {
			t.Errorf("%s never names BUILTIN\\Administrators", rel)
		}
		if !strings.Contains(code, "[System.IO.FileAttributes]::ReparsePoint") {
			t.Errorf("%s never tests for a reparse point (junction or symbolic link)", rel)
		}

		parent := strings.Index(code, "Protect-ItsDirectory -Path $DataParent")
		leaf := strings.Index(code, "Assert-NotReparsePoint $DataDir")
		mkdir := strings.Index(code, "New-Item -ItemType Directory -Force -Path $DataDir")
		token := strings.Index(code, "[System.IO.FileMode]::CreateNew")
		if parent < 0 || mkdir < 0 || parent > mkdir {
			t.Errorf("%s does not secure the data directory's parent before it creates the data directory "+
				"(Protect-ItsDirectory $DataParent at %d, New-Item $DataDir at %d)", rel, parent, mkdir)
		}
		if leaf < 0 || leaf > mkdir {
			t.Errorf("%s does not refuse a data directory that is a junction before it creates or uses it "+
				"(Assert-NotReparsePoint $DataDir at %d, New-Item $DataDir at %d)", rel, leaf, mkdir)
		}
		if token >= 0 && parent > token {
			t.Errorf("%s writes the token file before the data directory's parent is secured", rel)
		}
		// Only a parent named ITSuite is ours to lock down: a custom -DataDir
		// of C:\ITS must not end with a protected DACL on C:\.
		if !strings.Contains(text, "(Split-Path -Leaf $DataParent) -eq 'ITSuite'") {
			t.Errorf("%s secures the data directory's parent without checking it is ITSuite", rel)
		}
	}
}

// install.ps1 on a FRESH install (no service yet) refuses a parent or data
// directory that someone other than Administrators, SYSTEM or the installing
// user owns, as the self-installer does: taking ownership makes the directory
// ours from now on, but whatever that user planted in it before -- an
// identity.json, a pins.json -- survives the new ACL.
func TestInstallPs1RefusesAForeignOwnedDirectoryOnAFreshInstall(t *testing.T) {
	code := stripPowerShellLiterals(read(t, "windows/install.ps1"))
	svc := strings.Index(code, "$existing = Get-Service -Name $ServiceName")
	parent := strings.Index(code, "Protect-ItsDirectory -Path $DataParent")
	if svc < 0 || parent < 0 || svc > parent {
		t.Fatalf("install.ps1 must know whether this is a fresh install before it secures the parent "+
			"(Get-Service at %d, Protect-ItsDirectory at %d)", svc, parent)
	}
	if !strings.Contains(code, "-RefuseForeignOwner:$fresh") {
		t.Error("install.ps1 does not refuse a foreign-owned parent on a fresh install")
	}
	if !strings.Contains(code, "Assert-ItsOwner $DataDir") {
		t.Error("install.ps1 does not refuse a foreign-owned data directory on a fresh install")
	}
	for _, sid := range []string{"S-1-5-32-544", "S-1-5-18"} {
		if !strings.Contains(read(t, "windows/install.ps1"), sid) {
			t.Errorf("install.ps1's owner check does not accept %s", sid)
		}
	}
}
