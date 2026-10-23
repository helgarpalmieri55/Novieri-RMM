//go:build !linux && !windows && !darwin

package inventory

import "context"

// No OS to ask, so nothing is claimed. See facts_other.go: no cross-compile
// target reaches this file, and the collection refuses on os_family anyway.
func newOriginProber(context.Context) originProber { return noOriginProber{} }
