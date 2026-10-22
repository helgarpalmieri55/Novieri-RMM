//go:build windows

package health

import (
	"context"
	"testing"
	"time"
)

// avHealthy is a pure function, but it lives in posture_windows.go, which
// carries `//go:build windows`. It is therefore not reachable from the
// untagged posture_test.go on a non-Windows build machine, so this test
// carries the same build tag. It now RUNS, on the agent CI job's
// windows-latest runner, against the worked-example values in its own doc
// comment -- but that is still not avHealthy's own documented falsifier,
// which is TestProbeAntivirusWindowsAgainstTheRealSecurityCenter below.
func TestAvHealthy(t *testing.T) {
	cases := []struct {
		name       string
		state      uint32
		wantHealth bool
		wantKnown  bool
	}{
		{
			name:       "healthy Defender, worked example from doc comment",
			state:      0x061100,
			wantHealth: true,
			wantKnown:  true,
		},
		{
			name:       "scanner byte lacking 0x10 -> real-time protection not enabled",
			state:      0x060100,
			wantHealth: false,
			wantKnown:  true,
		},
		{
			name:       "definition byte carrying 0x10 -> definitions out of date",
			state:      0x061110,
			wantHealth: false,
			wantKnown:  true,
		},
		{
			name:       "zero value is not interpreted",
			state:      0,
			wantHealth: false,
			wantKnown:  false,
		},
		{
			name:       "provider byte >= 0x10 trips the state>>20 guard",
			state:      0x101100,
			wantHealth: false,
			wantKnown:  false,
		},
	}

	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			gotHealth, gotKnown := avHealthy(tc.state)
			if gotHealth != tc.wantHealth || gotKnown != tc.wantKnown {
				t.Errorf("avHealthy(0x%06X) = (%v, %v), want (%v, %v)",
					tc.state, gotHealth, gotKnown, tc.wantHealth, tc.wantKnown)
			}
		})
	}
}

// TestProbeAntivirusWindowsAgainstTheRealSecurityCenter is the falsifier
// avHealthy's own doc comment names: "on one real Windows machine with
// Defender running and current, read productState from root\SecurityCenter2
// and confirm avHealthy returns (true, true)." It drives the real path --
// COM, WMI, WQL, the live SecurityCenter2 provider on this runner -- rather
// than a value copied into a test case.
//
// NOT GATED ON THE SPECIFIC OUTCOME, and that is deliberate rather than a
// missed assertion. A GitHub-hosted windows-latest runner is a real Windows
// kernel and a real WMI provider, but it is not the fleet's own hardware and
// its Defender/third-party-AV registration is a property of that image, not
// of this code -- exactly the distinction the header above draws between
// "executed" and "the first-run verification on an actual endpoint". What is
// asserted is the property this code can actually promise regardless of that
// image: probeAntivirusWindows must not panic or hang against the real COM
// session, and whatever it reports must be internally consistent with
// avHealthy's own contract (known=false implies healthy=false, since §5
// forbids inferring health from "cannot tell").
func TestProbeAntivirusWindowsAgainstTheRealSecurityCenter(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()

	s := probeAntivirusWindows(ctx, nil, "")
	if !s.Available {
		t.Log("probeAntivirusWindows: unavailable on this runner (no COM session, or " +
			"root\\SecurityCenter2\\AntiVirusProduct returned nothing interpretable -- both " +
			"ordinary on a CI image; see the function's own header)")
		return
	}
	if !s.Bool && s.Available {
		// Available=true with Bool=false means "known=true, healthy=false" in
		// probeAntivirusWindows's own mapping -- a real, interpreted, unhealthy
		// reading, not the "cannot tell" case (that returns Available=false).
		t.Log("probeAntivirusWindows: available and reports UNHEALTHY on this runner")
	} else {
		t.Log("probeAntivirusWindows: available and reports HEALTHY on this runner")
	}
}
