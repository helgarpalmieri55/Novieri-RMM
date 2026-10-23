package winrm

import (
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"regexp"
	"strings"
	"testing"
)

// KERBEROS ONLY IS STRUCTURAL IN OUR OWN CODE (design §2.3): the files of THIS
// package must import no NTLM, Basic or CredSSP library, so there is no code
// path in our client that could negotiate one. This does NOT claim the whole
// dependency tree is NTLM-free (M2) — gokrb5's `spnego`/`service` graph pulls in
// gorilla/etc. and may transitively reference NTLM constants; that is not our
// code and not reachable from our client. What this guard pins is that WE add
// no such path. If a future edit adds `go-ntlmssp` or similar to a file here, it
// fails — the point being that refusal is the ABSENCE of the code we control.
func TestPackageImportsNoNonKerberosAuthLibrary(t *testing.T) {
	forbidden := []string{"ntlmssp", "ntlm", "credssp", "pkg/sspi", "go-ntlmssp"}
	fset := token.NewFileSet()
	files, _ := filepath.Glob("*.go")
	if len(files) == 0 {
		t.Fatal("no Go files found; the guard would be vacuous")
	}
	for _, f := range files {
		if strings.HasSuffix(f, "_test.go") {
			continue
		}
		tree, err := parser.ParseFile(fset, f, nil, parser.ImportsOnly)
		if err != nil {
			t.Fatal(err)
		}
		for _, imp := range tree.Imports {
			p := strings.ToLower(imp.Path.Value)
			for _, bad := range forbidden {
				if strings.Contains(p, bad) {
					t.Errorf("%s imports %s — a non-Kerberos auth path must not be compiled in", f, imp.Path.Value)
				}
			}
		}
	}
}

// The only Authorization header this package ever sets is `Negotiate <token>`,
// and nothing here sets a Basic credential on a request. Checked in the
// source, so an added `req.SetBasicAuth` or an `"NTLM "` header fails here
// rather than in review.
func TestTheOnlyAuthorizationSchemeSentIsNegotiate(t *testing.T) {
	files, _ := filepath.Glob("*.go")
	setAuth := regexp.MustCompile(`Header\.(Set|Add)\("Authorization",\s*([^)]*)`)
	seen := 0
	for _, f := range files {
		if strings.HasSuffix(f, "_test.go") {
			continue
		}
		src, err := os.ReadFile(f)
		if err != nil {
			t.Fatal(err)
		}
		if strings.Contains(string(src), "SetBasicAuth") {
			t.Errorf("%s sets HTTP Basic credentials", f)
		}
		for _, m := range setAuth.FindAllStringSubmatch(string(src), -1) {
			seen++
			if !strings.HasPrefix(strings.TrimSpace(m[2]), `"Negotiate "`) {
				t.Errorf("%s sets Authorization to %s; only Negotiate is permitted", f, m[2])
			}
		}
	}
	if seen == 0 {
		t.Fatal("no Authorization header found; the guard would be vacuous")
	}
}
