//go:build darwin

package health

// The posture sampler on macOS.
//
// THIS FILE HAS NEVER EXECUTED. No macOS machine exists in this project.
//
// What checks it, exactly: `GOOS=darwin go vet ./...`, which type-checks this
// package's darwin-tagged files INCLUDING its _test.go files, plus the
// darwin/amd64 and darwin/arm64 `go build ./cmd/agent` in the cross-compile
// step, which does not compile test files. Both run in the `agent` CI job --
// the vet only since 2026-08-13; before that darwin had no vet step at all and
// a darwin-tagged test could break unseen. That is compilation, not behaviour:
// a wrong `fdesetup` output shape type-checks perfectly.
//
// Two entries are deliberately absent, and an absent entry reports
// `unavailable` (§0.7):
//
//   - `posture.antivirus_healthy`: §4.1 makes it Windows-only. "AV health" on
//     macOS has no vendor-neutral answer.
//   - `disk.smart_ok`: §4.1's table marks it `—` on macOS.
func newPlatformPosture() Sampler {
	return newPostureSampler(execPostureRunner, map[string]probeFunc{
		"posture.disk_encryption":  probeDiskEncryptionDarwin,
		"posture.firewall_enabled": probeFirewallDarwin,
	})
}
