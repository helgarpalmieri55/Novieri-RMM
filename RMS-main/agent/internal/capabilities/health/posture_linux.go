//go:build linux

package health

// The posture sampler on Linux.
//
// `posture.antivirus_healthy` is deliberately ABSENT from this table: §4.1
// makes it Windows-only, because "AV health" on Linux has no vendor-neutral
// answer and inventing one produces a check that is confidently wrong. An
// absent entry reports `unavailable`, which is a visible, countable state.
func newPlatformPosture() Sampler {
	return newPostureSampler(execPostureRunner, map[string]probeFunc{
		"posture.disk_encryption":  probeDiskEncryptionLinux,
		"posture.firewall_enabled": probeFirewallLinux,
		"disk.smart_ok":            probeSMARTLinux,
	})
}
