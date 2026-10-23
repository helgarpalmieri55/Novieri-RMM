package enroll

import (
	"bytes"
	"crypto/tls"
	"crypto/x509"
	"testing"
	"time"
)

func identityWithValidity(notBefore, notAfter time.Time) *Identity {
	return &Identity{
		DeviceID: "d1",
		Cert: tls.Certificate{
			Leaf: &x509.Certificate{NotBefore: notBefore, NotAfter: notAfter},
		},
	}
}

func TestExpiryStateCrossesThresholdsFromTheCertificate(t *testing.T) {
	// Read from the parsed leaf, never from a constant: `ttl_days` is the
	// server's to choose (app/core/ca.py::sign_csr defaults to 30), and an
	// agent with 30 baked in would be silently wrong the day that changes.
	issued := time.Date(2026, 8, 1, 0, 0, 0, 0, time.UTC)
	expires := issued.Add(30 * 24 * time.Hour)
	id := identityWithValidity(issued, expires)

	day := func(n float64) time.Time {
		return issued.Add(time.Duration(n * float64(24*time.Hour)))
	}

	cases := []struct {
		name      string
		now       time.Time
		want      string
		remaining time.Duration
	}{
		{"fresh", issued, StateOK, 30 * 24 * time.Hour},
		{"day 19, still ok", day(19), StateOK, 11 * 24 * time.Hour},
		{"day 20, one third of life left", day(20), StateExpiring, 10 * 24 * time.Hour},
		{"day 29", day(29), StateExpiring, 24 * time.Hour},
		{"one second before NotAfter", expires.Add(-time.Second), StateExpiring, time.Second},
		{"exactly NotAfter", expires, StateExpired, 0},
		{"a day past", expires.Add(24 * time.Hour), StateExpired, -24 * time.Hour},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			remaining, state := ExpiryState(tc.now, id)
			if state != tc.want {
				t.Errorf("state = %q, want %q (remaining %v)", state, tc.want, remaining)
			}
			if remaining != tc.remaining {
				t.Errorf("remaining = %v, want %v", remaining, tc.remaining)
			}
		})
	}
}

func TestTheExpiringThresholdScalesWithTheCertificatesOwnLifetime(t *testing.T) {
	// The falsifier for "30 is hardcoded somewhere". A 90-day certificate must
	// go `expiring` at day 60, not at day 20.
	issued := time.Date(2026, 8, 1, 0, 0, 0, 0, time.UTC)
	id := identityWithValidity(issued, issued.Add(90*24*time.Hour))

	if _, state := ExpiryState(issued.Add(59*24*time.Hour), id); state != StateOK {
		t.Errorf("day 59 of 90 = %q, want %q", state, StateOK)
	}
	if _, state := ExpiryState(issued.Add(60*24*time.Hour), id); state != StateExpiring {
		t.Errorf("day 60 of 90 = %q, want %q", state, StateExpiring)
	}
}

func TestExpiryStateFailsClosedWithoutALeaf(t *testing.T) {
	// An identity with no parsed certificate cannot be shown to be valid, and
	// "unknown" would be reported to an operator as "fine". Fail to `expired`,
	// which is the loud state.
	for _, tc := range []struct {
		name string
		id   *Identity
	}{
		{"nil identity", nil},
		{"no leaf", &Identity{DeviceID: "d1"}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			remaining, state := ExpiryState(time.Now(), tc.id)
			if state != StateExpired {
				t.Fatalf("state = %q, want %q", state, StateExpired)
			}
			if remaining != 0 {
				t.Fatalf("remaining = %v, want 0", remaining)
			}
		})
	}
}

func TestExpiryStateAllocatesNothing(t *testing.T) {
	// The idle budget is a hard requirement, and this check runs on every
	// connect and on the daily tick. It is two time comparisons on an
	// already-parsed leaf; if it ever starts parsing PEM, this goes red.
	issued := time.Date(2026, 8, 1, 0, 0, 0, 0, time.UTC)
	id := identityWithValidity(issued, issued.Add(30*24*time.Hour))
	now := issued.Add(25 * 24 * time.Hour)

	if n := testing.AllocsPerRun(200, func() { _, _ = ExpiryState(now, id) }); n != 0 {
		t.Fatalf("ExpiryState allocated %v times per call; it must be free to call on every connect", n)
	}
}

func TestLogExpiryNamesTheExactNotAfterAtTheRightLevel(t *testing.T) {
	// The local log and the operator's device list (agents.cert_not_after is
	// already stored server-side) are the ONLY surfaces that exist for this.
	// There is no unsolicited channel to the control plane: JobLogMsg requires
	// a job_id bound to a real job for this device (_load_job_for_device), and
	// inventing one would be a second agent-initiated path with no
	// verification story. See docs/modules/agent/open-questions.md OQ-1.
	issued := time.Date(2026, 8, 1, 0, 0, 0, 0, time.UTC)
	expires := issued.Add(30 * 24 * time.Hour)
	id := identityWithValidity(issued, expires)

	cases := []struct {
		name      string
		now       time.Time
		wantLevel string
		// The remedy the line must carry. The two states are two different
		// facts and must not read the same: while `expiring`, rotation is
		// automatic and the operator should look at WHY it has not happened;
		// once `expired` there is no channel left to push a certificate down
		// and re-deployment is the only remedy. A line that told an operator to
		// re-deploy a machine that is about to renew itself is how they learn
		// to ignore the line that matters.
		wantRemedy string
		logged     bool
	}{
		{"ok says nothing", issued, "", "", false},
		{"expiring warns and does not say re-deploy",
			issued.Add(21 * 24 * time.Hour), "WARN", "renewal is automatic", true},
		{"expired errors and says re-deploy",
			expires.Add(time.Hour), "ERROR", "RE-DEPLOYED", true},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var buf bytes.Buffer
			LogExpiry(logTo(&buf), tc.now, id)
			out := buf.String()
			if !tc.logged {
				if out != "" {
					t.Fatalf("a healthy certificate logged: %s", out)
				}
				return
			}
			if !bytes.Contains(buf.Bytes(), []byte(tc.wantLevel)) {
				t.Errorf("level: want %s in %s", tc.wantLevel, out)
			}
			// The exact NotAfter, so an operator reading the endpoint's log
			// knows the date rather than "soon".
			if !bytes.Contains(buf.Bytes(), []byte(expires.Format(time.RFC3339))) {
				t.Errorf("log does not name NotAfter (%s): %s", expires.Format(time.RFC3339), out)
			}
			if !bytes.Contains(buf.Bytes(), []byte(tc.wantRemedy)) {
				t.Errorf("log does not carry the remedy %q for this state: %s", tc.wantRemedy, out)
			}
			if tc.wantLevel == "WARN" && bytes.Contains(buf.Bytes(), []byte("RE-DEPLOY")) {
				t.Errorf("an expiring certificate must not be reported as needing re-deployment; "+
					"rotation is automatic and retried on every connect: %s", out)
			}
		})
	}
}

func TestAnEnrolledIdentityCarriesAYearLongCertificate(t *testing.T) {
	// Ties the lifecycle code to the real thing: the fake control plane issues
	// exactly what app/core/ca.py::sign_csr does, which is now
	// AGENT_CERT_TTL_DAYS = 365 (docs/modules/agent/cert-renewal.md §0.1).
	//
	// The one-third threshold puts `expiring` at day 244 -- and the control
	// plane starts rotating at day 243, from the SAME one-third rule. So the
	// endpoint goes loud at roughly the moment the platform begins trying, by
	// construction rather than by coincidence.
	cp := newFakeCP(t)
	cfg, _ := cfgFor(t, cp)
	id, err := LoadOrEnroll(t.Context(), cfg, Options{})
	if err != nil {
		t.Fatalf("LoadOrEnroll: %v", err)
	}

	if _, state := ExpiryState(time.Now(), id); state != StateOK {
		t.Fatalf("a certificate issued seconds ago is %q", state)
	}
	if _, state := ExpiryState(time.Now().Add(250*24*time.Hour), id); state != StateExpiring {
		t.Fatalf("day 250 of 365 = %q, want %q -- the endpoint must be loud once the control "+
			"plane has been trying to rotate for a week", state, StateExpiring)
	}
	if _, state := ExpiryState(time.Now().Add(366*24*time.Hour), id); state != StateExpired {
		t.Fatalf("day 366 = %q, want %q", state, StateExpired)
	}
	if remaining, _ := ExpiryState(time.Now(), id); remaining > 366*24*time.Hour {
		t.Fatalf("remaining %v exceeds the CA's 365-day ttl", remaining)
	}
}
