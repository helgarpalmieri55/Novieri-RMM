//go:build linux

package backup

// The Linux dispatch's own test. `snapshot_linux.go` is two statements --
// requireKind, then the call into openLVM -- and those two statements are
// exactly what the untagged logic files deliberately do NOT cover: everything
// else about the LVM provider is exercised through the injected snapRunner
// seam in snapshot_lvm_test.go, which never goes through PlatformSnapshot at
// all. Before this file nothing covered them, on any platform.
//
// Nothing here runs a command or creates anything. Both cases return before
// the provider shells out, on purpose: a test that drove PlatformSnapshot at a
// real absolute root would, on a developer's LVM machine running as root,
// take an actual copy-on-write snapshot as a side effect. The one place that
// is allowed to touch real LVM is snapshot_lvm_integration_test.go, which
// builds its own loopback volume group and gates on both `lvcreate` and euid 0.

import (
	"context"
	"errors"
	"strings"
	"testing"

	"github.com/toeshee/it-suite/agent/internal/jobs"
)

func TestPlatformSnapshotOnLinuxRefusesAKindThisEndpointDoesNotProvide(t *testing.T) {
	// The kind arrives inside a SIGNED job. Substituting the locally correct
	// mechanism would discard a signed instruction; refusing puts the server's
	// misconfiguration in the job record where an operator sees it.
	for _, kind := range []string{snapshotVSS, snapshotAPFS, "nonsense"} {
		snap, err := PlatformSnapshot(context.Background(), kind, []string{"/data"})
		if snap != nil {
			t.Fatalf("PlatformSnapshot(%q) returned a usable snapshot on Linux", kind)
		}
		var refusal *jobs.Refusal
		if !errors.As(err, &refusal) {
			t.Fatalf("PlatformSnapshot(%q) = %v (%T), want a *jobs.Refusal", kind, err, err)
		}
		want := "backup.run: this endpoint provides the lvm snapshot mechanism, but the job asked for " + kind
		if refusal.Reason != want {
			t.Errorf("reason = %q, want %q", refusal.Reason, want)
		}
	}
}

func TestPlatformSnapshotOnLinuxDispatchesTheLVMKindToTheLVMProvider(t *testing.T) {
	// A relative root is refused by validateRoots, which only openLVM calls --
	// requireKind does not look at roots at all. So this reason can only have
	// been produced on the far side of the dispatch, which is what it proves.
	// It also means no command runs and nothing is created.
	snap, err := PlatformSnapshot(context.Background(), snapshotLVM, []string{"not/absolute"})
	if snap != nil {
		t.Fatal("PlatformSnapshot returned a usable snapshot for a relative root")
	}
	var refusal *jobs.Refusal
	if !errors.As(err, &refusal) {
		t.Fatalf("PlatformSnapshot(lvm) = %v (%T), want a *jobs.Refusal", err, err)
	}
	if !strings.Contains(refusal.Reason, "is not an absolute path") {
		t.Fatalf("reason = %q; want validateRoots' refusal, which only the LVM provider can produce. "+
			"A dispatch that never reached openLVM would answer something else entirely", refusal.Reason)
	}
	if strings.Contains(refusal.Reason, "snapshot mechanism") {
		t.Fatalf("reason = %q is requireKind's; the `lvm` kind must PASS the kind check on Linux", refusal.Reason)
	}
}
