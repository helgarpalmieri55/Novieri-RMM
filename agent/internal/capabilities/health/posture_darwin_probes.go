package health

// This file has NO build tag. Like posture_linux_probes.go it shells out only
// through the injected postureRunner, so every branch is exercised by a real
// `go test` on this project's Linux CI even though no macOS machine exists in
// this project.
//
// The name matters: Go applies an IMPLICIT build constraint to any file whose
// base name ends in a GOOS, so a `_darwin.go` suffix here would silently
// exclude this logic from every other platform's build and test run --
// exactly the untagged-everywhere property this file depends on. `_probes`
// is not a GOOS, so no implicit constraint applies.
//
// UNVERIFIED: the two command output FORMATS below are read from Apple's
// documentation and have never been observed on a real machine. What is tested
// here is the logic around them -- which strings mean on, which mean off, and
// that everything else is `unavailable`. If a format is wrong, the probe
// reports `unavailable` (no branch matches), which is visible and never opens
// or clears an alert. It does not silently report a healthy machine.

import (
	"context"
	"strings"
)

// probeDiskEncryptionDarwin answers for the system volume: FileVault is a
// single whole-container state, so there is no per-volume question to ask.
func probeDiskEncryptionDarwin(ctx context.Context, run postureRunner, _ string) Sample {
	res := run(ctx, "fdesetup", "status")
	if res.NotFound || !res.ok() {
		return unavailable()
	}
	out := strings.TrimSpace(res.Stdout)
	switch {
	case strings.HasPrefix(out, "FileVault is On."):
		return boolSample(true)
	case strings.HasPrefix(out, "FileVault is Off."):
		return boolSample(false)
	}
	// "Deferred enablement appears to be active for user X" is a real fdesetup
	// answer and it is NEITHER on nor off -- the volume is not encrypted yet
	// and will be at next logout. Reporting `false` would alarm on a machine
	// mid-enrolment; reporting `true` would hide one that never completes.
	return unavailable()
}

// socketfilterfwPath is the absolute path, not a PATH lookup. A root process
// resolving a bare name through PATH is a privilege-escalation shape, and there
// is exactly one socketfilterfw this can mean.
const socketfilterfwPath = "/usr/libexec/ApplicationFirewall/socketfilterfw"

// probeFirewallDarwin reads the application firewall's global state.
//
// State 1 (enabled for specific services) and state 2 (block all incoming) both
// count as enabled; only state 0 is disabled.
func probeFirewallDarwin(ctx context.Context, run postureRunner, _ string) Sample {
	res := run(ctx, socketfilterfwPath, "--getglobalstate")
	if res.NotFound || !res.ok() {
		return unavailable()
	}
	out := strings.ToLower(res.Stdout)
	switch {
	case strings.Contains(out, "state = 1"), strings.Contains(out, "state = 2"):
		return boolSample(true)
	case strings.Contains(out, "state = 0"):
		return boolSample(false)
	}
	return unavailable()
}
