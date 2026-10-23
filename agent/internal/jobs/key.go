package jobs

import "strings"

// capabilityKey is the serialization key for a job type: everything before the
// first dot, or the whole string when there is none.
//
// backup.run and backup.restore both key to "backup" ON PURPOSE. The shared
// mutable resource is the repository, not the verb -- keying on the exact job
// type would let a run write chunks while a restore reads the same repository,
// which surfaces as a corrupted restore rather than as a test failure.
//
// The index must be > 0, not >= 0: a type starting with a dot would otherwise
// key to "" and silently share a queue with every other malformed type.
func capabilityKey(jobType string) string {
	if i := strings.IndexByte(jobType, '.'); i > 0 {
		return jobType[:i]
	}
	return jobType
}
