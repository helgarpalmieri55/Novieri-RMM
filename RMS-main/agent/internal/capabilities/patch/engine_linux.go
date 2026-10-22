//go:build linux

package patch

import (
	"context"
	"os/exec"
)

// Linux selects apt, or refuses.
//
// The refusal is what a dnf, zypper, apk or pacman host gets, and it is a
// REFUSAL rather than a failure for the reason engine.go's errNoEngine
// documents: a host with no apt will not grow one by being retried, and the
// control plane should hear "this agent will not do this" once rather than
// re-dispatch a scan every night forever.
//
// spec.md §10 names apt/dnf/zypper for Linux and this build implements apt
// only. That is a stated gap (agent-capabilities.md §2), not an oversight: the
// fleet is Windows 11 and macOS, and a dnf parser written against output nobody
// here has ever captured would be a fixture-shaped guess wearing the clothes of
// coverage.
func osEngine(ctx context.Context) (engine, error) {
	if _, err := exec.LookPath(aptGet); err != nil {
		return nil, errNoEngine("apt-get is not on this host and it is the only Linux package " +
			"manager this build implements")
	}
	return newAPTEngine(), nil
}
