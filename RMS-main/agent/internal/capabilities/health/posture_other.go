//go:build !linux && !darwin && !windows

package health

// No posture probe exists for this platform. Every posture check reports
// `unavailable` (§0.7), which is a visible, countable state and never opens or
// clears an alert.
func newPlatformPosture() Sampler {
	return newPostureSampler(execPostureRunner, map[string]probeFunc{})
}
