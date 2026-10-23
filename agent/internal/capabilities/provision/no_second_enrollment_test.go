package provision

import (
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"testing"
)

// AN AGENT THAT CAN BE MADE TO ENROLL AGAIN IS AN AGENT WHOSE DEVICE IDENTITY
// IS NOT AN IDENTITY.
//
// `provision.enroll` is the job type in this platform whose NAME invites the
// mistake. It is not an enrollment; enrollment is its precondition. By the time
// this capability can run, the machine has already presented a certificate over
// mTLS and the runner has already verified a signature against the key pinned
// at that enrollment. There is nothing left to enroll.
//
// What a second enrollment path here would cost: a signed job -- or anything
// that could reach this code -- could mint a SECOND device identity for one
// machine. The control plane would then hold two Device rows, two Agent rows,
// two certificates and two capability lists for one endpoint; `enrollment.revoke_agent`
// would revoke one and leave the other commanding the machine; the audit trail
// would split in two; and `bind_provisioned_device` would correlate a token to
// whichever arrived first. The device_id is the anchor of invariant #5's
// tenancy scoping and of every job's `target_id`, and a capability that can
// change it is a capability that can rewrite who this machine is.
//
// So this package's ONLY permitted use of internal/enroll is reading: Load and
// ExpiryState. This walker is that rule, enforced on the source rather than
// promised in a comment. It is the same mechanism internal/transport uses for
// invariant #2, narrowed to this package, and it exists because the review that
// would otherwise catch this is a review of a file whose package name is
// "provision" and whose job type ends in ".enroll".

// mutatingEnrollSymbols are the entry points that CHANGE this agent's identity.
// Each carries the reason so a future reader knows exactly what they are
// permitting.
var mutatingEnrollSymbols = map[string]string{
	"enroll.Enroll":           "performs a full enrollment and would mint a second device identity",
	"enroll.LoadOrEnroll":     "enrolls when no identity is present, which is a full enrollment on any path that can clear one",
	"enroll.GenerateCSR":      "generates a NEW keypair and a CSR over it -- leg 1 of an identity change",
	"enroll.Install":          "swaps the identity on disk -- leg 2 of an identity change",
	"enroll.RevertToPrevious": "consumes the one-shot revert this endpoint owes its certificate rotation",
	"enroll.RecoverRotation":  "finishes an interrupted identity swap; it belongs to startup, not to a job",
}

// bannedMethods are matched on the METHOD NAME alone, whatever the receiver is
// called. `config.Config` is a value type, so `ConsumeEnrollToken` is reached
// through a local variable (`cfg.ConsumeEnrollToken()`) and a package-qualified
// rule would never see it -- which is precisely the kind of gap that makes a
// walker read as coverage while catching nothing.
var bannedMethods = map[string]string{
	"ConsumeEnrollToken": "deletes the installer's enrollment token. That is enrollment " +
		"bookkeeping, it is done exactly once by enroll.LoadOrEnroll at startup, and a job that " +
		"could do it would be a job that can destroy the credential a re-enrollment would need",
}

// networkConstructors are how a hand-rolled enrollment would be written if the
// enroll package were avoided. The control plane is reachable from this process
// and `EnrollPath` is an exported constant; a POST to it with a fresh CSR is
// about fifteen lines. None of them may appear here.
var networkConstructors = map[string]string{
	"http.NewRequest":               "builds an HTTP request; this capability speaks to the OS, never to the control plane",
	"http.NewRequestWithContext":    "builds an HTTP request; this capability speaks to the OS, never to the control plane",
	"http.Post":                     "posts to a URL; /enroll/v1 is a POST",
	"http.Get":                      "fetches a URL",
	"net.Dial":                      "opens a connection; the only connection this agent has is the gateway's, and it belongs to internal/transport",
	"tls.Dial":                      "opens a TLS connection outside internal/transport",
	"exec.Command":                  "spawns a process; this capability spawns none, on any platform (see the package comment)",
	"exec.CommandContext":           "spawns a process; this capability spawns none, on any platform (see the package comment)",
	"ed25519.GenerateKey":           "generates a keypair -- the first act of an identity change",
	"rsa.GenerateKey":               "generates a keypair -- the first act of an identity change",
	"ecdsa.GenerateKey":             "generates a keypair -- the first act of an identity change",
	"x509.CreateCertificateRequest": "builds a CSR",
}

func TestThisCapabilityHasNoSecondEnrollmentPathAndSpawnsNoProcess(t *testing.T) {
	// FALSIFYING OBSERVATION: the file, the line and the exact call. Not "no
	// error was returned" -- a call site.
	violations := walkForForbiddenCalls(t, ".")
	if len(violations) != 0 {
		t.Fatalf("this capability reached for an identity-changing or process-spawning call:\n  %s",
			strings.Join(violations, "\n  "))
	}
}

func TestTheWalkerItselfReportsAViolationWhenThereIsOne(t *testing.T) {
	// A walker nobody has ever seen report a violation is a walker nobody knows
	// works. This plants each forbidden call in a synthetic file and requires
	// the walker to name it -- the mutation-testing discipline applied to the
	// test that is doing the guarding.
	//
	// FALSIFYING OBSERVATION: a planted `enroll.Enroll(...)` that the walker
	// does not report, by name, with its line number.
	dir := t.TempDir()
	src := `package fake

import (
	"context"

	"github.com/toeshee/it-suite/agent/internal/config"
	"github.com/toeshee/it-suite/agent/internal/enroll"
)

func sneak(ctx context.Context, cfg config.Config) {
	_, _ = enroll.Enroll(ctx, cfg, enroll.Options{})
	_, _ = enroll.GenerateCSR(cfg)
	_ = cfg.ConsumeEnrollToken()
	_ = config.DirMode
}
`
	if err := os.WriteFile(filepath.Join(dir, "sneak.go"), []byte(src), 0o600); err != nil {
		t.Fatal(err)
	}
	got := walkForForbiddenCalls(t, dir)
	if len(got) != 3 {
		t.Fatalf("the walker reported %d violations for a file containing three forbidden CALLS: %v",
			len(got), got)
	}
	joined := strings.Join(got, "\n")
	for _, want := range []string{
		"enroll.Enroll", "enroll.GenerateCSR", "ConsumeEnrollToken",
		"sneak.go:11", "sneak.go:12", "sneak.go:13",
	} {
		if !strings.Contains(joined, want) {
			t.Errorf("the walker's report does not name %q:\n%s", want, joined)
		}
	}
	// `cfg.ConsumeEnrollToken()` on line 13 is the one that proves the
	// method-name rule works: it is reached through a local variable, so a
	// package-qualified rule alone would have reported two and looked healthy.
}

func TestTheWalkerReadsFilesBehindEveryBuildTag(t *testing.T) {
	// The two real OS implementations are `//go:build windows` and
	// `//go:build darwin`. CI runs on Linux, so the compiler never sees them --
	// and a `powershell.exe -Command Get-WinEvent` hidden behind a windows tag
	// is exactly the call this walker exists to catch and exactly the one a
	// GOOS-scoped check would miss.
	//
	// FALSIFYING OBSERVATION: the walker not visiting domain_windows.go.
	seen := map[string]bool{}
	err := filepath.Walk(".", func(path string, info os.FileInfo, err error) error {
		if err == nil && !info.IsDir() {
			seen[filepath.Base(path)] = true
		}
		return err
	})
	if err != nil {
		t.Fatal(err)
	}
	for _, f := range []string{"domain_windows.go", "domain_darwin.go", "domain_other.go"} {
		if !seen[f] {
			t.Errorf("%s is not in this package; the walker's coverage claim is about files that "+
				"must exist", f)
		}
	}
}

func TestTheReadOnlyEnrollEntryPointsAreStillAvailable(t *testing.T) {
	// The rule is "no MUTATING enroll symbol", not "no enroll import". If this
	// list ever became a blanket ban, `agent_healthy` would have to be answered
	// by something that does not know what an identity is, and the honest
	// answer would quietly become "true because I am running".
	for _, allowed := range []string{"enroll.Load", "enroll.ExpiryState"} {
		if _, banned := mutatingEnrollSymbols[allowed]; banned {
			t.Errorf("%s is in the banned list; it is READ-ONLY and identityHealth needs it", allowed)
		}
	}
}

func walkForForbiddenCalls(t *testing.T, root string) []string {
	t.Helper()
	fset := token.NewFileSet()
	var violations []string

	err := filepath.Walk(root, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		if info.IsDir() {
			if name := info.Name(); name == "testdata" || strings.HasPrefix(name, "_") {
				return filepath.SkipDir
			}
			return nil
		}
		// Test files are excluded: this rule is about what the SHIPPED
		// capability can do. A test that constructed an http.Request would not
		// be in the binary.
		if !strings.HasSuffix(path, ".go") || strings.HasSuffix(path, "_test.go") {
			return nil
		}
		// EVERY build tag, not just this platform's. `go vet` builds
		// domain_windows.go and domain_darwin.go for their own GOOS and this
		// walker must see them too -- a shell-out hidden behind `//go:build
		// windows` is exactly the one CI would otherwise never look at.
		file, perr := parser.ParseFile(fset, path, nil, parser.SkipObjectResolution)
		if perr != nil {
			return perr
		}
		rel, _ := filepath.Rel(root, path)
		ast.Inspect(file, func(n ast.Node) bool {
			call, ok := n.(*ast.CallExpr)
			if !ok {
				return true
			}
			name := selectorName(call.Fun)
			if why, bad := mutatingEnrollSymbols[name]; bad {
				violations = append(violations, fmt.Sprintf("%s:%d calls %s (%s)",
					rel, fset.Position(call.Pos()).Line, name, why))
			}
			if why, bad := networkConstructors[name]; bad {
				violations = append(violations, fmt.Sprintf("%s:%d calls %s (%s)",
					rel, fset.Position(call.Pos()).Line, name, why))
			}
			if sel, ok := call.Fun.(*ast.SelectorExpr); ok {
				if why, bad := bannedMethods[sel.Sel.Name]; bad {
					violations = append(violations, fmt.Sprintf("%s:%d calls .%s (%s)",
						rel, fset.Position(call.Pos()).Line, sel.Sel.Name, why))
				}
			}
			return true
		})
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	sort.Strings(violations)
	return violations
}

// selectorName renders `pkg.Fn` for a call expression, or "" for anything else.
func selectorName(e ast.Expr) string {
	sel, ok := e.(*ast.SelectorExpr)
	if !ok {
		return ""
	}
	ident, ok := sel.X.(*ast.Ident)
	if !ok {
		return ""
	}
	return ident.Name + "." + sel.Sel.Name
}
