package jobs_test

import (
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// Invariant #1, enforced by the type system rather than by review.
//
// VerifiedJob is an interface carrying an unexported method. A type declared
// outside internal/jobs cannot supply that method, so it cannot satisfy the
// interface, so it cannot be handed to a Handler. No capability package -- and
// no future contributor in a hurry -- can manufacture a job that claims to have
// been verified.
//
// The plan specified a STRUCT with an unexported field instead. That does not
// work: Go forbids specifying an unexported field from another package, not
// omitting it, and `jobs.VerifiedJob{ID: "forged"}` compiled cleanly on
// go1.24.7. This test is what caught it.
//
// testdata/badclient is a package that tries the forge. The go tool ignores
// directories named testdata, so it is never built by `go build ./...`,
// `go vet ./...` or staticcheck; this test builds it explicitly and asserts it
// FAILS.
//
// Falsifying observation: the build succeeds. That would mean a capability
// package could forge a verified job, and every check in verify.go would be
// bypassable by any code inside this binary.
func TestVerifiedJobCannotBeConstructedOutsideThisPackage(t *testing.T) {
	if _, err := exec.LookPath("go"); err != nil {
		t.Skip("no go toolchain on PATH")
	}
	cmd := exec.Command("go", "build", "-o", filepath.Join(t.TempDir(), "badclient"),
		"./testdata/badclient")
	out, err := cmd.CombinedOutput()
	if err == nil {
		t.Fatalf("testdata/badclient COMPILED. A package outside internal/jobs constructed a "+
			"VerifiedJob, so invariant #1 is enforced by convention only.\n%s", out)
	}
	// Assert on the reason, not merely on a non-zero exit: a typo in the
	// testdata package would also fail the build and would make this test pass
	// for the wrong reason.
	if !strings.Contains(string(out), "does not implement") ||
		!strings.Contains(string(out), "verifiedByTheSignedJobRunner") {
		t.Fatalf("badclient failed to build, but not because of the unexported field. "+
			"This test may be passing for the wrong reason:\n%s", out)
	}
	t.Logf("go build ./testdata/badclient failed as required:\n%s", out)
}
