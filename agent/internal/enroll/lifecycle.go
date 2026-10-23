// Certificate expiry awareness -- the endpoint's own view of a rotation that is
// not happening.
//
// # What this file is now, and what it used to be
//
// It was written when there was NO renewal path at all (OQ-1): certificates
// were 30 days, `grep -rn renew backend/app` found only license renewals, and
// this file existed to make that fuse visible on the surfaces that already
// existed. That gap is closed. Certificates are 365 days and are rotated
// automatically, starting at two-thirds of life, as two signed jobs
// (docs/modules/agent/cert-renewal.md; rotate.go is this endpoint's half).
//
// So the states mean something different. `expiring` no longer means "nobody is
// coming"; it means **the platform has been trying to rotate this certificate
// for a while and has not succeeded** -- the job was refused by an older agent,
// or the endpoint has not been online, or the CA refused the CSR. That is a
// genuine call to action, and it is why the remedy in the WARN says what it
// says rather than "re-enroll": telling an operator to re-deploy a machine that
// is about to renew itself is how you train them to ignore the line that
// matters.
//
// # Why the threshold is a FRACTION and not a number of days
//
// ttl_days is the server's to choose. An agent with "warn at 10 days left"
// baked in would be silently wrong -- and silently useless on a short
// certificate -- the day that default changed. One third of the lifetime is
// day 20 of 30 and day 243 of 365, and it is deliberately the same one-third
// rule the control plane uses to decide when to START rotating
// (app/services/cert_renewal.py::RENEWAL_REMAINING_DAYS), so the endpoint goes
// loud at roughly the moment the platform begins trying.
//
// # Still no out-of-band channel
//
// There is deliberately no attempt to tell the control plane about this.
// JobLogMsg requires a job_id bound to a real job for this device
// (dispatch.py::_load_job_for_device), so no unsolicited log channel exists,
// and inventing one would be a second agent-initiated path with no verification
// story. The server-side surface is `agents.cert_not_after`, which the device
// API now reads (cert-renewal.md §6).
package enroll

import (
	"log/slog"
	"time"
)

// Expiry states.
const (
	// StateOK: more than a third of the certificate's lifetime remains.
	StateOK = "ok"
	// StateExpiring: inside the last third. Loud, but the agent still works.
	StateExpiring = "expiring"
	// StateExpired: NotAfter has passed. The gateway will close 4401.
	StateExpired = "expired"
)

// expiringFraction is the share of a certificate's own lifetime that, once it
// is all that remains, makes the state `expiring`.
//
// A FRACTION, not a number of days. ttl_days is the server's to choose
// (app/core/ca.py::sign_csr defaults to 30 and takes it as a parameter), and an
// agent with "warn at 10 days left" baked in would be silently wrong -- and
// silently useless on a 7-day certificate -- the day that default changes. One
// third of a 30-day certificate is day 20.
const expiringFraction = 3

// ExpiryState reports how long this identity's client certificate has left and
// which of the three states that puts it in.
//
// Reads NotAfter (and NotBefore, for the lifetime) from the already-parsed
// leaf: no PEM decoding, no allocation, so it is free to call on every connect
// and on the daily scheduler tick rather than needing a timer of its own.
// lifecycle_test.go asserts zero allocations per call.
//
// Fails CLOSED. An identity with no parsed certificate cannot be shown to be
// valid, and reporting "unknown" would reach an operator as "fine".
func ExpiryState(now time.Time, id *Identity) (time.Duration, string) {
	if id == nil || id.Cert.Leaf == nil {
		return 0, StateExpired
	}
	leaf := id.Cert.Leaf
	remaining := leaf.NotAfter.Sub(now)
	if remaining <= 0 {
		return remaining, StateExpired
	}
	lifetime := leaf.NotAfter.Sub(leaf.NotBefore)
	if lifetime > 0 && remaining <= lifetime/expiringFraction {
		return remaining, StateExpiring
	}
	return remaining, StateOK
}

// LogExpiry writes one line, or none.
//
// Called at connect and on the daily tick. `ok` is silent on purpose: a line
// per connect for a healthy certificate is the noise that makes the WARN
// invisible when it finally appears.
func LogExpiry(log *slog.Logger, now time.Time, id *Identity) {
	remaining, state := ExpiryState(now, id)
	if state == StateOK {
		return
	}
	notAfter := "unknown"
	deviceID := ""
	if id != nil {
		deviceID = id.DeviceID
		if id.Cert.Leaf != nil {
			notAfter = id.Cert.Leaf.NotAfter.UTC().Format(time.RFC3339)
		}
	}
	// Two different remedies, because the two states are two different facts.
	//
	// While `expiring`, rotation is expected and automatic and the only thing an
	// operator should do is check WHY it has not happened -- the device's job
	// list and the audit log both name the answer. Naming re-deployment here
	// would be wrong most of the time.
	//
	// Once `expired` there is nothing left to try: the certificate is the
	// credential the rotation would have travelled over, the gateway closes 4401,
	// and re-deployment is genuinely the only remedy.
	const expiringRemedy = "renewal is automatic and is retried on every connect " +
		"(docs/modules/agent/cert-renewal.md); if this persists, the device's job list will show a " +
		"refused or failed agent.cert.renew / agent.cert.install"
	const expiredRemedy = "the gateway will close this agent 4401 and there is no channel left to " +
		"push a certificate down; this device must be RE-DEPLOYED"
	switch state {
	case StateExpiring:
		log.Warn("client certificate is expiring and has not been rotated",
			"device_id", deviceID, "not_after", notAfter,
			"remaining", remaining.Round(time.Hour).String(), "remedy", expiringRemedy)
	case StateExpired:
		log.Error("client certificate has EXPIRED; the gateway will refuse this agent",
			"device_id", deviceID, "not_after", notAfter,
			"overdue", (-remaining).Round(time.Hour).String(), "remedy", expiredRemedy)
	}
}
