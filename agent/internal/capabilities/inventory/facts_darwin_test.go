//go:build darwin

package inventory

import (
	"context"
	"testing"
	"time"
)

// TestGatherAgainstTheRealMacOSCommands runs facts_darwin.go's gather()
// unmodified, against the real sw_vers, ioreg and sysctl on this runner.
//
// The parsers (parseSWVers, parseIORegSerial in macfacts.go) already have
// fixture-driven coverage that runs on any GOOS, including linux CI -- see
// parse_test.go. What that coverage cannot reach is exec.go's run(): whether
// `sw_vers`, `ioreg -rd1 -c IOPlatformExpertDevice` and
// `sysctl -n machdep.cpu.brand_string` / `-n hw.memsize` are the right argv
// for a REAL macOS, in a REAL PATH, producing output the parsers actually
// accept. Until this job started running on macos-latest, nothing here had
// ever executed on a Mac.
//
// A VM/CI Mac may report an empty Serial (no IOPlatformSerialNumber on some
// virtualized IOPlatformExpertDevice nodes) -- run()'s own doc comment names
// that as an ordinary, ungoverned absence, so it is logged rather than
// required. Hostname, OS version and CPU model are not optional: every real
// Mac has sw_vers, sysctl and a CPU brand string, so an empty one of those
// means the exec path itself is broken, not that the fact was unavailable.
func TestGatherAgainstTheRealMacOSCommands(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	f := gather(ctx)

	if f.Hostname == "" {
		t.Error("Hostname is empty; os.Hostname() failed on a real Mac")
	}
	if f.Architecture == "" {
		t.Error("Architecture is empty; runtime.GOARCH should never be")
	}
	if f.OSVersion == "" {
		t.Error("OSVersion is empty; sw_vers is a core macOS binary and should have answered")
	}
	if f.CPUModel == "" {
		t.Error("CPUModel is empty; `sysctl -n machdep.cpu.brand_string` should answer on real hardware")
	}
	if f.RAMBytes <= 0 {
		t.Error("RAMBytes is not positive; `sysctl -n hw.memsize` should answer on real hardware")
	}
	t.Logf("real macOS gather(): hostname=%q fqdn=%q os=%q build=%q arch=%q serial=%q cpu=%q ram=%d components=%d",
		f.Hostname, f.FQDN, f.OSVersion, f.OSBuild, f.Architecture, f.Serial, f.CPUModel, f.RAMBytes, len(f.Components))
	if f.Serial == "" {
		t.Log("Serial is empty -- ordinary on a VM with no IOPlatformSerialNumber; not gated")
	}
}
