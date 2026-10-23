package packaging_test

import (
	"strings"
	"testing"
)

// Review I4 (install link, 2026-09-28). A DACL does not protect a data
// directory a local user PRE-CREATED: that user is its owner, and an owner
// always keeps WRITE_DAC. Both scripts that write the token therefore take
// ownership for BUILTIN\Administrators, and neither writes into a token file
// that already exists -- a pre-created `enroll-token` keeps its creator's
// owner and ACL, and WriteAllText truncates it rather than replacing it. The
// token file is removed and then created with CreateNew, which fails if it
// reappeared in between.
//
// The self-installer does the same in Go (internal/install: secdir.SecureOwned
// and O_EXCL); the install-link e2e reads the owner and the DACL back on a real
// Windows runner. The data directory's PARENT gets the same: parent_test.go.
func TestWindowsTokenWritersOwnTheDataDirectoryAndCreateTheTokenFileFresh(t *testing.T) {
	for _, rel := range []string{"windows/install.ps1", "windows/its-agent-enroll.ps1"} {
		text := read(t, rel)
		owner := strings.Index(text, "$acl.SetOwner((New-Object System.Security.Principal.NTAccount('BUILTIN\\Administrators')))")
		setACL := strings.Index(text, "Set-Acl -Path $DataDir -AclObject $acl")
		remove := strings.Index(text, "Remove-Item -LiteralPath $tokenFile -Force")
		create := strings.Index(text, "[System.IO.FileMode]::CreateNew")
		if owner < 0 || setACL < 0 || owner > setACL {
			t.Errorf("%s does not set the data directory's owner to BUILTIN\\Administrators before Set-Acl "+
				"(SetOwner at %d, Set-Acl at %d)", rel, owner, setACL)
		}
		if remove < 0 || create < 0 || remove > create {
			t.Errorf("%s does not remove the token file and then create it with CreateNew "+
				"(Remove-Item at %d, CreateNew at %d)", rel, remove, create)
		}
		if strings.Contains(text, "WriteAllText($tokenFile") {
			t.Errorf("%s still writes the token with WriteAllText, which writes into a pre-created file", rel)
		}
		if create >= 0 && setACL > create {
			t.Errorf("%s creates the token file before the data directory's ACL is set", rel)
		}
	}
}
